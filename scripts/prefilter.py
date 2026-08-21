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
    return PrefilterResult("pass" if matched else "fail", RULESET_V1, text, matched)


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

    counts = {"pass": 0, "fail": 0, "unknown": 0}
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
    lines = [f"stage 0 over {total} accounts:"]
    for k in ("pass", "fail", "unknown"):
        lines.append(f"  {k:<8} {counts.get(k,0):>4}  ({100*counts.get(k,0)/total:.0f}%)")
    lines.append(
        f"  full research budget would go to {counts.get('pass',0)} of {total} "
        f"({100*counts.get('pass',0)/total:.0f}%)"
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
                 include: tuple[str, ...] = ("fail", "unknown")) -> list[dict]:
    """Companies needing a judgment, as a batch to hand to a classifier."""
    where = ["status != 'excluded'"]
    params: list = []
    if fund:
        where.append("fund = ?")
        params.append(fund)
    marks = ",".join("?" * len(include))
    where.append(f"(prefilter IS NULL OR prefilter IN ({marks}))")
    params.extend(include)
    rows = conn.execute(
        f"SELECT id, name, domain, what FROM accounts WHERE {' AND '.join(where)}"
        f" ORDER BY id LIMIT ?", (*params, limit)
    ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "domain": r["domain"],
         "description": r["what"] or ""}
        for r in rows
    ]


VALID_VERDICTS = ("pass", "fail", "unknown")


def import_verdicts(conn, verdicts: list[dict], rule: str = RULESET_LLM) -> dict[str, int]:
    """Load classifier output. Rejects anything malformed rather than guessing."""
    counts = {"pass": 0, "fail": 0, "unknown": 0}
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
        if verdict == "pass" and not reason:
            raise ValueError(f"id {vid}: a pass needs a reason naming the evidence")
        counts[verdict] += 1
        conn.execute(
            "UPDATE accounts SET prefilter = ?, prefilter_rule = ?, prefilter_evidence = ?,"
            " prefilter_at = ? WHERE id = ?",
            (verdict, rule, reason, utcnow(), vid),
        )
    log_event(conn, "info", "prefilter.import", rule=rule, **counts)
    return counts


CLASSIFY_BRIEF = """\
For each company below decide whether it plausibly has people doing AI/ML research
or ML engineering -- someone whose job involves building or training models.

Judge the company, not the sentence. The text is marketing copy written to sell a
product, not to describe an engineering org. A company whose entire business is an
ML model can describe itself as "game-ready on-demand 3D assets" and never use the
word AI. Consider what the product must be built out of.

Say `pass` when there plausibly is such a function, `fail` when there plausibly is
not, `unknown` when the text does not support either. Prefer `unknown` over a
confident `fail`: a wrongly dropped company leaves no evidence that it was wrongly
dropped, while a wrongly kept one costs one research budget.

A research function in an unrelated field is a `fail` for our purposes -- a
zero-knowledge cryptography team is a research team, but not one that wants an AI
collaboration.

Return JSON: [{"id": <int>, "verdict": "pass"|"fail"|"unknown", "reason": "<what in
the text or in what you know about the company supports this>"}]
"""
