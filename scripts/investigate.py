"""An investigation loop, not a channel sequence.

The channels in this skill each fail on some population: personal pages fail on
hardware engineers, paper first pages fail on companies that do not publish,
GitHub patterns fail without a public org, rosters fail where no org exists.
Running them in a fixed order and reporting what each one missed produces an
accurate list of gaps and very few contacts.

What works instead is treating every partial result as a lead. One person found
by a dork is a doorway, not a contact: their page links Scholar, Scholar lists
papers, a paper's first page carries emails and an affiliation line naming the
employer, and its coauthors are usually colleagues -- so one entry point becomes
a team, and each record is grounded in a dated primary document that names both
the person and the company. That is not weaker than a roster intersection. It is
stronger, because a paper is evidence about a moment and a membership list is
evidence about now with no history.

So the loop asks one question at each step: *what is the next investigation that
gets me closer to a grounded contact?* A dead end is information about where to
look next, not a stopping condition.

It stops on budget, or on `max_dry` consecutive steps that yield neither a fact
nor a lead. Both are needed: budget alone lets a rich seed spend everything on
one company, and dryness alone never terminates on a graph that keeps offering
new coauthors.

Every step is logged -- what was tried, what it gave, what it suggested next --
because the reasoning is the reviewable part. A contact whose derivation cannot
be read is a contact that cannot be checked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import meter

# Lead kinds, most-promising first. The loop always takes the highest-value lead
# available rather than draining one kind at a time.
PRIORITY = ("person", "homepage", "scholar", "paper", "coauthor", "domain_pattern",
            "title_hunt", "enrichment")

# A pattern is only worth applying to someone it was not learned from when it is
# both confident and well-sampled. `first` at 55% from 11 addresses describes a
# domain with no convention, and deriving from it produces plausible-looking
# addresses that bounce.
MIN_PATTERN_CONFIDENCE = 0.9
MIN_PATTERN_SAMPLES = 5


@dataclass
class Fact:
    """A grounded claim. Never stored without a URL and a quote."""
    kind: str            # email | title | affiliation
    subject: str         # person's name
    value: str
    url: str
    quote: str

    def as_evidence(self, retrieved_at: str) -> dict:
        return {"claim": f"{self.subject}: {self.kind} is {self.value}",
                "url": self.url, "quote": self.quote[:400],
                "retrieved_at": retrieved_at}


@dataclass
class Lead:
    kind: str
    value: str
    subject: str = ""     # which person this is about, when known
    why: str = ""         # how we got here, for the log
    depth: int = 0

    def key(self) -> tuple:
        return (self.kind, self.value.lower(), self.subject.lower())


@dataclass
class Step:
    lead: Lead
    outcome: str
    facts: list[Fact] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    note: str = ""

    @property
    def productive(self) -> bool:
        return bool(self.facts or self.leads)


@dataclass
class Investigation:
    company: str
    domain: str
    budget: int = 60
    max_dry: int = 8
    max_depth: int = 4
    steps: list[Step] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    frontier: list[Lead] = field(default_factory=list)
    seen: set = field(default_factory=set)
    stopped_because: str = ""

    # ------------------------------------------------------------- frontier

    def push(self, lead: Lead) -> bool:
        if lead.depth > self.max_depth or lead.key() in self.seen:
            return False
        self.seen.add(lead.key())
        self.frontier.append(lead)
        return True

    def pop_best(self) -> Lead | None:
        if not self.frontier:
            return None
        self.frontier.sort(key=lambda l: (PRIORITY.index(l.kind)
                                          if l.kind in PRIORITY else 99, l.depth))
        return self.frontier.pop(0)

    # ------------------------------------------------------------ knowledge

    def person_facts(self, name: str) -> dict[str, Fact]:
        out: dict[str, Fact] = {}
        for f in self.facts:
            if f.subject.lower() == name.lower() and f.kind not in out:
                out[f.kind] = f
        return out

    def people(self) -> list[str]:
        return sorted({f.subject for f in self.facts if f.subject})

    def complete(self, name: str) -> bool:
        """Has an address and an affiliation. Title is chased but not required."""
        got = self.person_facts(name)
        return "email" in got and "affiliation" in got

    def inferred_only(self, name: str) -> bool:
        """Address derived from a domain convention, never seen for this person.

        Kept distinct from `complete` because the two are different claims: one
        says "this address exists", the other says "an address of this shape
        would exist if the convention holds". Review weighs them differently and
        so must this.
        """
        got = self.person_facts(name)
        return "email" not in got and "email_inferred" in got

    # ------------------------------------------------------------------ log

    def render_log(self) -> str:
        lines = [f"# Investigation: {self.company} ({self.domain})", "",
                 f"{len(self.steps)} steps, {len(self.facts)} grounded facts, "
                 f"{len(self.people())} people touched.",
                 f"Stopped because: {self.stopped_because}", ""]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"### {i}. {s.lead.kind}: {s.lead.value[:88]}")
            if s.lead.why:
                lines.append(f"- reached from: {s.lead.why}")
            lines.append(f"- outcome: {s.outcome}")
            for f in s.facts:
                lines.append(f"  - **{f.kind}** {f.subject} = `{f.value}`  ({f.url})")
            if s.leads:
                nxt = ", ".join(f"{l.kind}:{l.value[:40]}" for l in s.leads[:5])
                lines.append(f"- next: {nxt}"
                             + (f" (+{len(s.leads) - 5} more)" if len(s.leads) > 5 else ""))
            if s.note:
                lines.append(f"- note: {s.note}")
            lines.append("")
        return "\n".join(lines)

    def write_log(self, run_id: str) -> Path:
        d = Path(__file__).resolve().parent.parent / "state" / "investigations"
        d.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", self.company.lower()).strip("-")
        p = d / f"{run_id}_{slug}.md"
        p.write_text(self.render_log())
        return p


# ---------------------------------------------------------------- the steps
#
# Each takes (investigation, lead) and returns a Step. A step that finds nothing
# still returns leads where it can, because "no email here, but here is their
# Scholar page" is the loop's most common productive move.


def step_person(inv: Investigation, lead: Lead) -> Step:
    """Open a named person: find their page, and queue what it points at."""
    from .person_pages import find

    r = find(lead.value, company=inv.company)
    facts, leads = [], []
    if r.status == "found" and r.emails:
        facts.append(Fact("email", lead.value, r.emails[0], r.url or "",
                          f"address published on {r.url}"))
    if r.url:
        leads.append(Lead("homepage", r.url, lead.value,
                          f"personal page for {lead.value}", lead.depth + 1))
    if not r.url:
        # No page. The paper channel does not need one.
        leads.append(Lead("paper", lead.value, lead.value,
                          f"no personal page for {lead.value}; try their papers",
                          lead.depth + 1))
    outcome = {"found": "page with an address", "no_email": "page, no address",
               "namesake_risk": "page did not corroborate the employer",
               "not_found": "no page"}.get(r.status, r.status)
    return Step(lead, outcome, facts, leads,
                note=f"tried {len(r.tried)} url(s), source={r.source or 'n/a'}")


def step_homepage(inv: Investigation, lead: Lead) -> Step:
    """Read a page we already have: emails, Scholar link, affiliation sentence."""
    from .homepages import fetch_one, visible_text
    from .person_pages import emails_on

    r = fetch_one(lead.value)
    if r.status not in ("ok", "js_shell") or not r.raw:
        return Step(lead, f"fetch {r.status}", note=r.detail)
    text = r.text or visible_text(r.raw)
    facts, leads = [], []

    for e in emails_on(r.raw, text)[:1]:
        facts.append(Fact("email", lead.subject, e, lead.value,
                          f"address published on {lead.value}"))

    # An affiliation sentence naming the company is the corroboration the
    # evidence contract wants, and it often carries the title in the same clause.
    first = inv.company.split()[0].lower()
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split()))[:60]:
        if first in sentence.lower() and lead.subject.split()[0].lower() in sentence.lower() \
                or (first in sentence.lower() and len(sentence) < 220):
            facts.append(Fact("affiliation", lead.subject, inv.company,
                              lead.value, sentence[:300]))
            m = re.search(r"\b(research scientist|research engineer|member of technical "
                          r"staff|applied scientist|software engineer|researcher|"
                          r"engineer|scientist|professor|intern)\b", sentence, re.I)
            if m:
                facts.append(Fact("title", lead.subject, m.group(1).title(),
                                  lead.value, sentence[:300]))
            break

    for m in re.finditer(r'https?://scholar\.google\.[^"\'\s<>]+', r.raw):
        leads.append(Lead("scholar", m.group(0), lead.subject,
                          f"linked from {lead.value}", lead.depth + 1))
        break
    if not any(f.kind == "email" for f in facts):
        leads.append(Lead("paper", lead.subject, lead.subject,
                          f"{lead.value} had no address; try their papers",
                          lead.depth + 1))
    return Step(lead, f"read page ({len(text)} chars)", facts, leads)


def step_scholar(inv: Investigation, lead: Lead) -> Step:
    """Scholar rarely gives an address, but it confirms affiliation and names papers."""
    from .homepages import fetch_one, visible_text

    r = fetch_one(lead.value)
    if r.status not in ("ok", "js_shell") or not r.raw:
        return Step(lead, f"fetch {r.status}",
                    leads=[Lead("paper", lead.subject, lead.subject,
                                "scholar unreachable; go to papers directly",
                                lead.depth + 1)],
                    note=r.detail)
    text = r.text or visible_text(r.raw)
    facts = []
    m = re.search(r"Verified email at ([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text)
    if m:
        facts.append(Fact("email_domain", lead.subject, m.group(1), lead.value,
                          f"Verified email at {m.group(1)}"))
    return Step(lead, "read scholar profile", facts,
                [Lead("paper", lead.subject, lead.subject,
                      f"papers listed on {lead.value}", lead.depth + 1)])


def step_paper(inv: Investigation, lead: Lead) -> Step:
    """Open this person's recent papers: addresses on page one, coauthors as leads."""
    from .paper_emails import emails_in, first_page_text, pair, search, PaperHit

    results = search(f'au:"{lead.subject or lead.value}"', max_results=4)
    if not results:
        return Step(lead, "no papers found")
    facts, leads = [], []
    read = 0
    for aid, title, authors in results[:2]:
        text = first_page_text(aid)
        read += 1
        if not text:
            continue
        url = f"https://arxiv.org/abs/{aid}"
        hit = PaperHit(aid, title, emails_in(text, inv.domain), authors)
        if hit.emails:
            attributed, _ = pair(hit)
            for email, author in attributed.items():
                facts.append(Fact("email", author, email, url,
                                  f"address on the first page of \"{title}\""))
                # The affiliation line is what makes this a grounded record.
                facts.append(Fact("affiliation", author, inv.company, url,
                                  f"listed with an {inv.domain} address on \"{title}\""))
        # Coauthors on a paper carrying company addresses are usually colleagues.
        if hit.emails:
            for a in authors:
                if a != lead.subject:
                    leads.append(Lead("person", a, a,
                                      f"coauthor of \"{title[:48]}\" with "
                                      f"{inv.domain} addresses", lead.depth + 1))
    return Step(lead, f"read {read} paper first page(s)", facts, leads)


def step_domain_pattern(inv: Investigation, lead: Lead) -> Step:
    """Learn the domain's convention so a name becomes an address."""
    from pathlib import Path as _P

    from .config import Config
    from .github_harvest import (Client, apply_pattern, harvest_domain,
                                 infer_pattern, resolve_org)

    cfg = Config(_P(__file__).resolve().parent.parent / "config")
    c = Client(token=cfg.secrets().get("GITHUB_TOKEN"))
    org, why = resolve_org(c, inv.company, inv.domain)
    if not org:
        return Step(lead, "no public GitHub org", note=why)
    res = harvest_domain(c, inv.company, inv.domain, repos=cfg.campaign.github_repos)
    if not res.addresses:
        return Step(lead, f"org {org} had no usable addresses", note=res.status)
    pattern, conf, samples = infer_pattern(res.addresses)
    facts, leads = [], []
    observed_names = {re.sub(r"[^a-z]", "", n.lower())
                      for n, _ in res.addresses.values()}
    for email, (name, when) in res.addresses.items():
        if len(name.split()) >= 2:
            quote = (f"commit authored by {name} <{email}> in the {org} GitHub "
                     f"organisation, most recent {when[:10]}")
            facts.append(Fact("email", name, email, f"https://github.com/{org}", quote))
            # The same commit grounds the affiliation: a dated act, authored from
            # an address at the company's domain, in the company's own org. That
            # is a primary document naming both the person and the employer --
            # the thing the evidence contract asks for. It says nothing about
            # their role, which is why title_hunt still runs.
            facts.append(Fact("affiliation", name, inv.company,
                              f"https://github.com/{org}", quote))
            leads.append(Lead("title_hunt", name, name,
                              f"have an address for {name}, need a role",
                              lead.depth + 1))
    # Apply the pattern, do not merely report it. Learning that a domain uses
    # first.last at 100% and then doing nothing with it means the pattern half of
    # this channel produces a number in a report and no contacts. Everyone on the
    # company's current roster who has no observed address gets one derived here,
    # marked inferred so review can weigh it differently.
    inferred = 0
    if pattern and conf >= MIN_PATTERN_CONFIDENCE and len(samples) >= MIN_PATTERN_SAMPLES:
        from .hf_org import current_at

        for member in current_at(inv.company).values():
            key = re.sub(r"[^a-z]", "", member["name"].lower())
            if key in observed_names:
                continue
            guess = apply_pattern(pattern, member["name"], inv.domain)
            if not guess:
                continue
            inferred += 1
            facts.append(Fact(
                "email_inferred", member["name"], guess,
                f"https://github.com/{org}",
                f"derived from the {inv.domain} convention {pattern!r}, measured at "
                f"{conf:.0%} across {len(samples)} observed addresses in the {org} "
                f"GitHub organisation; {member['name']} is a current member of the "
                f"company's Hugging Face org"))
            # Deliberately no lead. A large roster produces a hundred inferred
            # addresses, and queueing a person lead for each floods the frontier
            # and spends the budget confirming guesses instead of finding real
            # addresses -- which cost three observed contacts on the first run
            # that did it. The fact is recorded; confirming it is a separate
            # decision the operator makes at review.

    note = f"org={org}; {len(res.addresses)} observed"
    if inferred:
        note += f", {inferred} inferred from the pattern"
    return Step(lead, f"pattern {pattern} at {conf:.0%} from {len(samples)} sample(s)",
                facts, leads, note=note)


def step_title_hunt(inv: Investigation, lead: Lead) -> Step:
    """Chase a missing title. Never infers one from activity.

    A title is a claim about someone's role; commit history is evidence about
    their activity. Turning the second into the first is the kind of confident
    wrong answer this system keeps having to catch, so an unfindable title stays
    unknown and is flagged at review for the operator to decide per row.
    """
    from .hf_org import check

    ok, why = check(inv.company, lead.subject)

    # No org, or one too thin to testify: fall back to OpenAlex, ranked on
    # sustained recency rather than its own ordering. That ranking matters --
    # reading last_known_institutions[0] put people at the wrong employer
    # entirely, because the list is unordered and a single recent year is
    # usually a parsing artefact.
    if ok is None:
        try:
            from .openalex import Client, current_affiliation

            from .config import Config as _C
            from pathlib import Path as _P
            cfg = _C(_P(__file__).resolve().parent.parent / "config")
            client = Client(mailto=cfg.campaign.contact_email or None)
            author = client.find_author(lead.subject, affiliation_hint=inv.company)
            if author:
                inst, conf, reason = current_affiliation(author)
                if inst and conf >= 0.5:
                    return Step(lead, f"openalex affiliation: {inst} ({conf:.0%})",
                                [Fact("affiliation", lead.subject, inst, author.url,
                                      f"OpenAlex affiliations ranked on sustained "
                                      f"recency: {reason}")],
                                [Lead("person", lead.subject, lead.subject,
                                      f"need a title for {lead.subject}",
                                      lead.depth + 1)],
                                note=why)
        except Exception as exc:
            return Step(lead, f"roster could not answer; openalex fallback failed "
                              f"({type(exc).__name__})", note=why)

    facts = []
    if ok:
        facts.append(Fact("affiliation", lead.subject, inv.company,
                          "https://huggingface.co/organizations/"
                          f"{__import__('scripts.hf_org', fromlist=['x']).slug_for(inv.company)}",
                          why))
    leads = [Lead("person", lead.subject, lead.subject,
                  f"need a title for {lead.subject}; try their own page",
                  lead.depth + 1)]
    return Step(lead, "roster check" if ok is not None else "roster could not answer",
                facts, leads, note=why)


def step_enrichment(inv: Investigation, lead: Lead) -> Step:
    """Paid lookup, if the operator ever configures one."""
    from . import enrichment

    if not enrichment.available():
        return Step(lead, "skipped: no paid provider configured",
                    note="see scripts/enrichment.py to add one")
    r = enrichment.resolve(lead.subject, inv.company, inv.domain)
    if not r:
        return Step(lead, "provider had nothing")
    facts = [Fact("email", lead.subject, r.email, r.source_url, r.quote)] if r.email else []
    return Step(lead, f"provider {r.provider}", facts)


STEPS = {
    "person": step_person,
    "homepage": step_homepage,
    "scholar": step_scholar,
    "paper": step_paper,
    "domain_pattern": step_domain_pattern,
    "title_hunt": step_title_hunt,
    "enrichment": step_enrichment,
}


def run(company: str, domain: str, seeds: list[Lead], *, budget: int = 60,
        max_dry: int = 8, max_depth: int = 4) -> Investigation:
    inv = Investigation(company, domain, budget, max_dry, max_depth)
    for s in seeds:
        inv.push(s)

    dry = 0
    while len(inv.steps) < budget:
        lead = inv.pop_best()
        if lead is None:
            inv.stopped_because = "frontier empty: every lead was followed"
            break
        try:
            step = STEPS[lead.kind](inv, lead)
        except Exception as exc:
            step = Step(lead, f"error: {type(exc).__name__}", note=str(exc)[:160])
        inv.steps.append(step)
        meter.bump("investigation_steps")

        for f in step.facts:
            inv.facts.append(f)
        for nxt in step.leads:
            inv.push(nxt)

        dry = 0 if step.productive else dry + 1
        if dry >= max_dry:
            inv.stopped_because = (f"{dry} consecutive steps yielded neither a fact "
                                   f"nor a lead")
            break
    else:
        inv.stopped_because = f"budget of {budget} steps exhausted"
    return inv
