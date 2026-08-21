"""Stage 0: is this company addressable at all?

A fund portfolio is mostly not our target. Roughly four in five a16z entries are
crypto, consumer, fintech, games or health companies with no research or ML
engineering function to collaborate with. Spending a 15-call research budget on
each of 667 companies is 10,000 tool calls to find ~130 real targets.

So a cheap pass runs first. Three properties matter:

**It is recoverable.** The verdict, the ruleset that produced it, and the exact
text judged are all stored, and the raw fund payload is cached. Stage 0 can be
re-run with better rules without re-fetching anything, and a `fail` is a verdict
rather than a deletion.

**`unknown` is not `fail`.** A company with no description was not judged, it was
skipped. Those queue for a cheap search rather than being dropped.

**It is measured.** A wrongly-dropped company produces no evidence that it was
wrongly dropped, so the false-negative rate cannot be caught in review and has to
be sampled deliberately. See `outbound prefilter --sample-failures`.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .db import log_event, utcnow

# Signals that a company has people who do research or ML engineering. Kept
# broad on purpose: a false negative costs a real target and leaves no trace,
# while a false positive only costs one research budget.
KEYWORDS_V1 = (
    r"\bA\.?I\.?\b", r"\bmachine learning\b", r"\bML\b", r"\bLLM\b", r"\bmodels?\b",
    r"\bneural\b", r"\binference\b", r"\bagents?\b", r"\bfoundation model", r"\bdeep learning\b",
    r"\bNLP\b", r"\bcomputer vision\b", r"\bresearch\b", r"\balgorithms?\b",
    r"\bautonom", r"\brobot", r"\bsimulation\b", r"\bdata science\b", r"\btraining\b",
    r"\bgenerative\b", r"\btransformer", r"\bembedding", r"\breinforcement learning\b",
    r"\bperception\b", r"\bforecast", r"\boptimi[sz]ation\b", r"\bcomputational\b",
)
RULESET_V1 = "keywords_v1"
RULESET_LLM = "llm_v1"

# `pass` is split by depth because the two need different copy. A company that
# trains its own models and publishes is a different conversation from one that
# ships a feature on somebody else's API, and one blended verdict makes the two
# reply rates unreadable.
VALID_VERDICTS = ("pass_builds", "pass_applies", "fail", "unknown")
PASS_VERDICTS = ("pass_builds", "pass_applies")
DEPTH = {"pass_builds": "builds", "pass_applies": "applies"}
VALID_EVIDENCE = ("description", "search")
URL_OR_DOMAIN = re.compile(r"https?://\S+|\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}\b", re.I)

_COMPILED = [re.compile(k, re.I) for k in KEYWORDS_V1]


@dataclass
class PrefilterResult:
    verdict: str            # pass | fail | unknown
    rule: str
    evidence: str
    matched: list[str] = field(default_factory=list)


def judge(name: str, description: str | None, extra: str | None = None) -> PrefilterResult:
    """Judge one company from text already in hand. No network, no model."""
    text = " ".join(x for x in (name, description, extra) if x).strip()
    if not description:
        # Not judged. A company with no marketing copy is not a company without
        # an engineering org.
        return PrefilterResult("unknown", RULESET_V1, text)
    matched = [c.pattern for c in _COMPILED if c.search(text)]
    # keywords_v1 cannot tell depth apart; only the classifier can.
    return PrefilterResult("pass_applies" if matched else "fail", RULESET_V1, text, matched)


def apply(conn: sqlite3.Connection, *, fund: str | None = None,
          only_unjudged: bool = False, rule: str = RULESET_V1) -> dict[str, int]:
    """Run stage 0 over accounts. Idempotent and re-runnable."""
    where = ["status != 'excluded'"]
    params: list = []
    if fund:
        where.append("fund = ?")
        params.append(fund)
    if only_unjudged:
        where.append("(prefilter IS NULL OR prefilter_rule != ?)")
        params.append(rule)
    rows = conn.execute(
        f"SELECT id, name, what FROM accounts WHERE {' AND '.join(where)}", tuple(params)
    ).fetchall()

    counts = {k: 0 for k in VALID_VERDICTS}
    for r in rows:
        res = judge(r["name"], r["what"])
        counts[res.verdict] += 1
        conn.execute(
            "UPDATE accounts SET prefilter = ?, prefilter_rule = ?, prefilter_evidence = ?,"
            " prefilter_at = ? WHERE id = ?",
            (res.verdict, res.rule, res.evidence[:2000], utcnow(), r["id"]),
        )
    log_event(conn, "info", "prefilter.apply", fund=fund, rule=rule, **counts)
    return counts


def summary(counts: dict[str, int]) -> str:
    total = sum(counts.values()) or 1
    passes = sum(counts.get(k, 0) for k in PASS_VERDICTS) + counts.get("pass", 0)
    lines = [f"stage 0 over {total} accounts:"]
    for k in ("pass", "pass_builds", "pass_applies", "fail", "unknown"):
        if counts.get(k):
            lines.append(f"  {k:<13} {counts[k]:>4}  ({100*counts[k]/total:.0f}%)")
    lines.append(
        f"  full research budget would go to {passes} of {total} "
        f"({100*passes/total:.0f}%)"
    )
    if counts.get("unknown"):
        lines.append(
            f"  {counts['unknown']} have no description and were not judged -- they queue "
            f"for a cheap search rather than being dropped"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- llm ruleset
#
# The keyword ruleset was measured against a hand-checked sample and dropped
# roughly a third of real targets. `website_description` is marketing copy: it
# sells a product and does not describe an engineering org, so a company whose
# whole business is an ML model can describe itself as "game-ready on-demand 3D
# assets" and score zero keyword hits.
#
# Deciding whether marketing copy implies an ML function is judgment, so it
# belongs on the agentic side. The split is the same contract used for candidate
# discovery: a script exports a batch, the model judges, a script validates and
# loads the verdicts. Nothing here calls a model itself.


def export_batch(conn, *, fund: str | None = None, limit: int = 200,
                 offset: int = 0,
                 include: tuple[str, ...] = ("fail", "unknown", "pass_applies")) -> list[dict]:
    """Companies needing a judgment, as a batch to hand to a classifier."""
    where = ["status != 'excluded'"]
    params: list = []
    if fund:
        where.append("fund = ?")
        params.append(fund)
    marks = ",".join("?" * len(include))
    # An `unknown` is unresolved by definition, so it comes back when better
    # evidence arrives -- even if the classifier is what produced it.
    where.append(
        f"(prefilter IS NULL OR prefilter = 'unknown'"
        f" OR (prefilter_rule != '{RULESET_LLM}' AND prefilter IN ({marks})))"
    )
    params.extend(include)
    rows = conn.execute(
        f"SELECT id, name, domain, what, homepage_text, homepage_fetch_status"
        f" FROM accounts WHERE {' AND '.join(where)}"
        f" ORDER BY id LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "domain": r["domain"],
            # The company's own words, which beat the investor's blurb.
            "homepage": (r["homepage_text"] or "")[:1200],
            "homepage_status": r["homepage_fetch_status"],
            "blurb": r["what"] or "",
        }
        for r in rows
    ]




def import_verdicts(conn, verdicts: list[dict], rule: str = RULESET_LLM) -> dict[str, int]:
    """Load classifier output. Rejects anything malformed rather than guessing."""
    counts = {k: 0 for k in VALID_VERDICTS}
    for v in verdicts:
        if not isinstance(v, dict):
            raise ValueError(f"verdict is not an object: {v!r}")
        vid, verdict = v.get("id"), v.get("verdict")
        if not isinstance(vid, int):
            raise ValueError(f"verdict missing an integer id: {v!r}")
        if verdict not in VALID_VERDICTS:
            raise ValueError(
                f"id {vid}: verdict {verdict!r} is not one of {VALID_VERDICTS}"
            )
        reason = str(v.get("reason") or "")[:500]
        evidence = v.get("evidence")

        if verdict in PASS_VERDICTS:
            if not reason:
                raise ValueError(f"id {vid}: a pass needs a reason naming the evidence")
            if evidence not in VALID_EVIDENCE:
                raise ValueError(
                    f"id {vid}: a pass needs evidence to be one of {VALID_EVIDENCE}, "
                    f"got {evidence!r}"
                )
            # A search can return a different company with the same name -- three
            # of sixteen did in one hand-checked sample. A pass grounded in the
            # wrong Mosaic spends a full research budget on a company that is not
            # in the portfolio, so search-grounded passes must cite where they
            # looked.
            if evidence == "search" and not URL_OR_DOMAIN.search(reason):
                raise ValueError(
                    f"id {vid}: a search-grounded pass must cite a URL or the company's "
                    f"domain in its reason, so the result can be checked against the "
                    f"right company. Got: {reason!r}"
                )

        counts[verdict] += 1
        conn.execute(
            "UPDATE accounts SET prefilter = ?, prefilter_rule = ?, prefilter_evidence = ?,"
            " prefilter_at = ?, ai_depth = ? WHERE id = ?",
            (verdict, rule, reason, utcnow(), DEPTH.get(verdict), vid),
        )
        # Fund portfolio companies are startups; tier is what routes a campaign.
        # ai_depth stays separate so the two pitches report apart.
        if verdict in PASS_VERDICTS:
            conn.execute(
                "UPDATE accounts SET tier = COALESCE(tier, 'startup'),"
                " campaign = COALESCE(campaign, 'startup') WHERE id = ? AND fund IS NOT NULL",
                (vid,),
            )
    log_event(conn, "info", "prefilter.import", rule=rule, **counts)
    return counts


CLASSIFY_BRIEF = """\
For each company below decide whether it plausibly has people doing AI/ML research
or ML engineering -- someone whose job involves building or training models.

Each entry carries two texts. `homepage` is the company's own words and is the
better evidence. `blurb` is its investor's one-line description, which is written
to position a portfolio and routinely omits what the company is built out of --
Kaedim's blurb was "game-ready on-demand 3D assets" while its own homepage opens
"AI-powered 3D asset creation".

`homepage_status` says whether the homepage produced usable text. Anything other
than `ok` means the site did not render for us, which is a fact about the fetch
and not about the company: judge from the blurb alone, and prefer `unknown`.

Judge the company, not the sentence. The text is marketing copy written to sell a
product, not to describe an engineering org. A company whose entire business is an
ML model can describe itself as "game-ready on-demand 3D assets" and never use the
word AI. Consider what the product must be built out of.

Verdicts:

  pass_builds   trains or fine-tunes its own models, has research staff, or
                publishes. Kaedim (image-to-3D reconstruction) and Santa Ana Bio
                (ML models for immuno-oncology, published in Nature) are this.
  pass_applies  ships AI features built on somebody else's models. Valon
                (LLM integration for mortgage servicing) and Tako (applied AI on
                a payroll product) are this.
  fail          plausibly no such function.
  unknown       the text does not support either.

Prefer `unknown` over a confident `fail`: a wrongly dropped company leaves no
evidence that it was wrongly dropped, while a wrongly kept one costs one research
budget. But do not reach for `unknown` when the company is simply outside the
field -- a sourdough delivery service is a `fail`, not an ambiguity.

A research function in an unrelated field is a `fail` for our purposes -- a
zero-knowledge cryptography team is a research team, but not one that wants an AI
collaboration.

Return JSON, one object per company:

  {"id": <int>,
   "verdict": "pass_builds" | "pass_applies" | "fail" | "unknown",
   "evidence": "description" | "search",     // required on a pass
   "reason": "<what supports this>"}

`evidence: "description"` means the supplied text was enough. `evidence: "search"`
means you looked the company up -- and then the reason must cite a URL or the
company's own domain, because a search on a common name returns a different
company with that name often enough to matter. A pass grounded in the wrong
Mosaic spends a full research budget on a company that is not in the portfolio.
"""
