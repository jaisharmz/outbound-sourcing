"""Company discovery: three modes, one table.

    list      a file of company names the operator supplies
    vc        portfolio pages, researched agentically, handed here as a list
    industry  wraps the `industry-research` skill

All three land in `accounts`. Nothing downstream cares which mode produced a row.

    python -m scripts.discover_companies --mode industry --run <dir>
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Config, load_config
from .db import log_event, open_db, transaction, utcnow
from .errors import ConfigError
from .normalize import normalize_company, registrable_domain

# A landscape `url` is whatever the researcher cited as evidence. For NVIDIA that
# is nvidia.com; for Google DeepMind it is an arXiv abstract, for Anthropic a
# docs subdomain, for a university group a personal site or a GitHub repo.
# Extracting a sending domain from those produces mail to a stranger at the
# wrong company, so anything on this list yields no domain candidate at all.
AGGREGATOR_DOMAINS = {
    "arxiv.org", "github.com", "github.io", "gitlab.com", "huggingface.co",
    "semanticscholar.org", "openreview.net", "biorxiv.org", "medrxiv.org",
    "doi.org", "acm.org", "ieee.org", "springer.com", "nature.com",
    "sciencedirect.com", "wikipedia.org", "linkedin.com", "x.com", "twitter.com",
    "youtube.com", "medium.com", "substack.com", "notion.site",
    "crunchbase.com", "pitchbook.com", "theinformation.com", "techcrunch.com",
    "bloomberg.com", "reuters.com", "propublica.org", "forbes.com", "wired.com",
    "arstechnica.com", "grantmaking.ai",
}

# Hosts that belong to a real company but host third-party content, so a link to
# one says nothing about whose company it describes. Checked before the
# registrable domain, since google.com itself is a legitimate target.
AGGREGATOR_HOSTS = {
    "docs.google.com", "drive.google.com", "sites.google.com",
    "colab.research.google.com", "groups.google.com", "gist.github.com",
}



# A source run's `excluded` list answers "is this company part of this field?",
# which is a topical judgment and not a statement about prospect quality. Pinecone
# is excluded from a memory map for shipping a vector index rather than a
# retention policy; that says nothing about whether it wants a collaboration. So
# exclusions are imported for triage, not dropped -- except for the kinds that are
# genuinely never targets, which are named by pattern in icp.yaml.

CATEGORY_MARKERS = (
    "generally", "cohort", "and similar", "the application layer", "harnesses",
    "and the ", "layer generally", "et al",
)


def _is_category(fragment: str) -> bool:
    """True when a fragment names a category rather than a company."""
    f = fragment.strip()
    if not f or len(f.split()) > 5:
        return True
    if f[:1].islower():
        return True
    return any(m in f.lower() for m in CATEGORY_MARKERS)


def split_company_names(name: str) -> list[str]:
    """Split a grouped label into individual companies.

    Roughly a quarter of real `excluded` entries name several companies in one
    string -- "Pinecone, Weaviate, LangChain, LlamaIndex" or "Hailo, Axelera AI,
    Blaize". Those are not valid account rows. Parenthetical content is handled
    separately, since it is sometimes a qualifier ("Protect AI (Palo Alto
    Networks)") and sometimes where the actual list lives ("Coding agent
    harnesses generally (Cursor, OpenClaw and similar)").
    """
    raw = name.strip()
    paren = re.findall(r"\(([^)]*)\)", raw)
    outer = re.sub(r"\s*\([^)]*\)", "", raw).strip()

    def parts(text: str) -> list[str]:
        pieces = re.split(r",\s*|\s+and\s+", text)
        return [p.strip(" .,") for p in pieces if p.strip(" .,")]

    outer_parts = parts(outer)
    if len(outer_parts) > 1:
        kept = [p for p in outer_parts if not _is_category(p)]
        if len(kept) > 1:
            return kept
        if kept and not paren:
            return kept

    # The outer text was a single label. If it is a category and the parentheses
    # hold a list, that list is the real content.
    if paren and _is_category(outer):
        inner = [p for p in parts(paren[0]) if not _is_category(p)]
        if len(inner) > 1:
            return inner

    return [raw]


def auto_drop_reason(reason: str | None, patterns: list[str]) -> str | None:
    """Return the pattern that marks this exclusion as never-a-target."""
    if not reason:
        return None
    low = reason.lower()
    for pat in patterns:
        if pat.lower() in low:
            return pat
    return None


@dataclass
class CompanyRecord:
    name: str
    tier: str | None = None
    campaign: str | None = None
    domain: str | None = None
    domain_confidence: str = "unknown"
    what: str | None = None
    entry_note: str | None = None
    ships: bool | None = None
    subproblems: list[str] = field(default_factory=list)
    evidence_url: str | None = None
    source: str = "list"
    source_ref: str | None = None
    fund: str | None = None
    stages: str | None = None
    verticals: str | None = None
    year_founded: str | None = None
    founders: list[str] = field(default_factory=list)
    excluded_reason: str | None = None
    # Why a source run set this company aside. Kept as context for triage, not
    # as a rejection.
    source_note: str | None = None


@dataclass
class DiscoveryReport:
    source: str = ""
    source_ref: str = ""
    added: int = 0
    updated: int = 0
    excluded: int = 0
    skipped_tier: list[str] = field(default_factory=list)
    no_domain: list[str] = field(default_factory=list)
    no_tier: list[str] = field(default_factory=list)
    needs_triage: list[str] = field(default_factory=list)
    auto_dropped: list[str] = field(default_factory=list)
    split_names: list[str] = field(default_factory=list)
    founders_found: int = 0
    degraded_run: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"source:    {self.source} ({self.source_ref})",
            f"accounts:  {self.added} added, {self.updated} updated, "
            f"{self.excluded} recorded as excluded",
        ]
        if self.skipped_tier:
            shown = ", ".join(sorted(set(self.skipped_tier))[:6])
            lines.append(f"skipped by tier: {len(self.skipped_tier)} ({shown})")
        if self.no_domain:
            lines.append(
                f"no domain candidate: {len(self.no_domain)} -- these need resolution "
                f"before people discovery: {', '.join(self.no_domain[:5])}"
                + (" ..." if len(self.no_domain) > 5 else "")
            )
        if self.founders_found:
            lines.append(
                f"founders named by the fund: {self.founders_found} -- recorded in "
                f"known_people, so discovery resolves an email instead of finding a person"
            )
        if self.split_names:
            lines.append(f"grouped names split into rows: {len(self.split_names)}")
            for x in self.split_names[:4]:
                lines.append(f"    {x}")
        if self.auto_dropped:
            lines.append(f"auto-dropped as never-a-target: {len(self.auto_dropped)}")
            for x in self.auto_dropped[:4]:
                lines.append(f"    {x}")
        if self.needs_triage:
            lines.append(
                f"source-run exclusions kept for triage: {len(self.needs_triage)} "
                f"(topical judgments, not prospect quality) -- outbound accounts --needs-triage"
            )
        if self.no_tier:
            lines.append(
                f"no tier: {len(self.no_tier)} imported name-only and cannot enroll in a "
                f"campaign until a tier is assigned (outbound accounts --needs-triage)"
            )
        if self.degraded_run:
            lines.append(
                f"DEGRADED SOURCE RUN: {self.degraded_run}\n"
                f"  Its roster is a floor, not a census. Companies nobody mentioned were "
                f"never found. Accounts are marked degraded and re-queue."
            )
        for w in self.warnings:
            lines.append(f"warning: {w}")
        return "\n".join(lines)


# ------------------------------------------------------------------ industry


def read_report_json(run_dir: Path) -> dict[str, Any] | None:
    """`report.json`, the cleanest surface when a run has one.

    It carries the same `orgs` and `excluded` structures as `landscape.md` but as
    real JSON, so it cannot be lost to a YAML quoting mistake in prose. Present
    in 6 of 11 runs sampled.
    """
    path = run_dir / "report.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("orgs"):
        return None
    return data


def _salvage_orgs(text: str) -> tuple[list[dict], int]:
    """Parse a list-of-mappings block one record at a time.

    A single malformed record otherwise costs the entire block, and therefore an
    entire run. One real file contains `what: "Critique of World Model," at v5
    ...` -- a quoted scalar followed by bare text -- which is a YAML error that
    took out 760 lines and about thirty companies. Skip the bad record, keep the
    rest, and report how many were dropped.
    """
    items, dropped, current = [], 0, []

    def flush():
        nonlocal dropped
        if not current:
            return
        try:
            parsed = yaml.safe_load("\n".join(current))
        except yaml.YAMLError:
            dropped += 1
            return
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            items.append(parsed[0])
        else:
            dropped += 1

    for line in text.splitlines():
        if re.match(r"^\s*- ", line):
            flush()
            current = [line]
        elif current:
            current.append(line)
    flush()
    return items, dropped


def read_yaml_block(path: Path, report: "DiscoveryReport | None" = None) -> dict[str, Any] | None:
    """Pull the fenced YAML block out of an industry-research markdown file."""
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("```yaml"))
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("```"))
    except StopIteration:
        return None
    body = lines[start + 1:end]
    try:
        data = yaml.safe_load("\n".join(body))
        if isinstance(data, dict):
            return data
    except yaml.YAMLError:
        pass

    # Whole-block parse failed. Recover the sections we actually need.
    out: dict[str, Any] = {}
    sections: dict[str, list[str]] = {}
    key = None
    for line in body:
        # A top-level key starts at column zero. One carrying an inline value
        # (`investors: []`) still ends the previous section -- otherwise it gets
        # swallowed into it and corrupts the last record there.
        m = re.match(r"^(\w+):(.*)$", line)
        if m:
            key = m.group(1) if not m.group(2).strip() else None
            if key:
                sections[key] = []
        elif key:
            sections[key].append(line)
    total_dropped = 0
    for name in ("orgs", "excluded"):
        if name in sections:
            items, dropped = _salvage_orgs("\n".join(sections[name]))
            out[name] = items
            total_dropped += dropped
    if not out:
        return None
    if report is not None:
        report.warnings.append(
            f"{path.name} has a YAML syntax error; recovered "
            f"{len(out.get('orgs', []))} orgs record-by-record and dropped {total_dropped}"
        )
    return out


def read_run_json(run_dir: Path) -> dict[str, Any]:
    """Read run.json leniently.

    Its real shape has drifted from the schema in the skill's own docs -- it
    gained `degraded`, `verification`, `corrections_applied`, `integrity_warning`
    and `known_gaps`, and lost the documented `profile_hash`. Depend only on the
    keys that have held.
    """
    path = run_dir / "run.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# Two kinds of URL, and confusing them is how a company ends up with the wrong
# sending domain.
#
#   DECLARED   the source says "this is the company's website" -- a fund
#              portfolio entry, a `Name,domain` line in a list file. Take it.
#   CITED      the source offers it as evidence for a claim -- a landscape
#              `url`, which for Google DeepMind is an arXiv abstract. Screen it.
#
# The aggregator screen belongs only on the cited path. Running it on a declared
# field discards Medium and Substack for being on the blogging-platform list.


def declared_domain(url: str | None) -> str | None:
    """Registrable domain of a URL the source declares to be the company's own.

    No aggregator screen: the source already asserted whose site this is.
    """
    if not url or not isinstance(url, str):
        return None
    match = re.match(r"^https?://([^/\s:]+)", url.strip())
    if not match:
        return None
    return registrable_domain(match.group(1).lower().removeprefix("www."))


def domain_from_cited_url(url: str | None) -> tuple[str | None, str]:
    """Derive a sending domain from an evidence URL, or refuse to.

    Only for URLs a source *cited*, never for one it declared. See above.

    Returns (domain, confidence). Confidence is never better than 'candidate':
    the URL was cited as evidence for a claim, not offered as a homepage.
    """
    if not url or not isinstance(url, str):
        return None, "unknown"
    match = re.match(r"^https?://([^/\s:]+)", url.strip())
    if not match:
        return None, "unknown"
    host = match.group(1).lower().removeprefix("www.")
    reg = registrable_domain(host)
    if host in AGGREGATOR_HOSTS or reg in AGGREGATOR_DOMAINS or host.endswith(".github.io"):
        return None, "aggregator"
    # A path-bearing docs or blog subdomain is still that company's domain, but
    # it is not necessarily where their mail lives.
    return reg, "candidate"


def from_industry_run(config: Config, run_dir: Path, tiers: set[str]) -> tuple[list[CompanyRecord], DiscoveryReport]:
    run_dir = Path(run_dir).expanduser()
    if not run_dir.is_dir():
        raise ConfigError(f"not a directory: {run_dir}")

    report = DiscoveryReport(source="industry", source_ref=str(run_dir))
    meta = read_run_json(run_dir)
    if meta.get("degraded"):
        detail = meta["degraded"]
        note = detail.get("websearch") if isinstance(detail, dict) else str(detail)
        report.degraded_run = str(note)[:200]

    block = read_report_json(run_dir)
    if block:
        report.warnings.append("read orgs from report.json")
    else:
        block = read_yaml_block(run_dir / "landscape.md", report)
    if not block:
        report.warnings.append(
            f"{run_dir/'landscape.md'} has no parseable YAML block; falling back to the "
            f"key_companies lists in the avenue frontmatter, which carry names but no URLs"
        )
        return _from_avenue_frontmatter(run_dir, tiers, report), report

    orgs = block.get("orgs") or []
    if not isinstance(orgs, list):
        report.warnings.append("landscape.md `orgs` is not a list; nothing imported")
        return [], report

    records: list[CompanyRecord] = []
    for org in orgs:
        if not isinstance(org, dict) or not org.get("name"):
            continue
        tier = str(org.get("tier") or "").strip()
        if tiers and tier not in tiers:
            report.skipped_tier.append(f"{org['name']} ({tier or 'no tier'})")
            continue
        domain, confidence = domain_from_cited_url(org.get("url"))
        if not domain:
            report.no_domain.append(str(org["name"]))
        sub = org.get("subproblems")
        records.append(CompanyRecord(
            name=str(org["name"]),
            tier=tier or None,
            campaign=config.campaigns.for_tier(tier) if tier else None,
            domain=domain,
            domain_confidence=confidence,
            what=str(org["what"])[:1000] if org.get("what") else None,
            entry_note=str(org["entry"])[:1000] if org.get("entry") else None,
            ships=bool(org["ships"]) if isinstance(org.get("ships"), bool) else None,
            subproblems=[str(x) for x in sub] if isinstance(sub, list) else [],
            evidence_url=str(org.get("evidence") or org.get("url") or "") or None,
            source="industry",
            source_ref=str(run_dir),
        ))

    # The source run's exclusions. Imported for triage rather than dropped: its
    # inclusion test is topical and answers a different question than ours.
    drop_patterns = config.icp.auto_drop_reason_patterns
    for ex in block.get("excluded") or []:
        if not isinstance(ex, dict) or not ex.get("name"):
            continue
        why = str(ex.get("why") or "excluded by the source run")[:500]
        matched = auto_drop_reason(why, drop_patterns)
        names = split_company_names(str(ex["name"]))
        if len(names) > 1:
            report.split_names.append(f"{ex['name']} -> {', '.join(names)}")
        for n in names:
            if matched:
                report.auto_dropped.append(f"{n} (matched {matched!r})")
                records.append(CompanyRecord(
                    name=n, source="industry", source_ref=str(run_dir),
                    evidence_url=str(ex.get("evidence") or "") or None,
                    excluded_reason=f"auto-dropped, matched {matched!r}: {why}",
                ))
            else:
                report.needs_triage.append(n)
                records.append(CompanyRecord(
                    name=n, source="industry", source_ref=str(run_dir),
                    evidence_url=str(ex.get("evidence") or "") or None,
                    source_note=f"excluded by the source run as off-topic for it: {why}",
                ))
    return records, report


def _from_avenue_frontmatter(run_dir: Path, tiers: set[str],
                             report: DiscoveryReport) -> list[CompanyRecord]:
    """Fallback: key_companies from each avenue's YAML header. Names only."""
    names: set[str] = set()
    for path in sorted((run_dir / "avenues").glob("*.md")) if (run_dir / "avenues").is_dir() else []:
        text = path.read_text()
        match = re.match(r"\A---\n(.*?)\n---", text, re.S)
        if not match:
            continue
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            continue
        for n in fm.get("key_companies") or []:
            names.add(str(n).strip())
    if not names:
        report.warnings.append(f"{run_dir/'avenues'} yielded no key_companies either")
    report.no_tier.extend(sorted(names))
    return [
        CompanyRecord(name=n, source="industry", source_ref=str(run_dir),
                      domain_confidence="unknown")
        for n in sorted(names)
    ]


# ------------------------------------------------------------------ list / vc


def from_name_list(path: Path, source: str, config: Config,
                   tier: str | None = None) -> tuple[list[CompanyRecord], DiscoveryReport]:
    """A newline-delimited file of names, or `name,domain` pairs."""
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"company list not found: {path}")
    report = DiscoveryReport(source=source, source_ref=str(path))
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, domain = line.partition(",")
        records.append(CompanyRecord(
            name=name.strip(),
            domain=domain.strip().lower() or None,
            domain_confidence="declared" if domain.strip() else "unknown",
            tier=tier,
            campaign=config.campaigns.for_tier(tier) if tier else None,
            source=source,
            source_ref=str(path),
        ))
    return records, report



# ------------------------------------------------------------------ vc funds


def from_fund(config: Config, fund_name: str, *, force: bool = False,
              limit: int | None = None) -> tuple[list[CompanyRecord], DiscoveryReport]:
    """Read one fund's portfolio. Strategy comes from config/funds.yaml."""
    from . import funds as funds_mod

    if fund_name not in config.funds.funds:
        raise ConfigError(
            f"no fund named {fund_name!r} in funds.yaml. "
            f"Known: {sorted(config.funds.funds) or '(none)'}"
        )
    spec = config.funds.funds[fund_name].model_dump()
    report = DiscoveryReport(source="vc", source_ref=fund_name)
    try:
        companies = funds_mod.extract(fund_name, spec, force=force, limit=limit)
    except funds_mod.FundError as exc:
        raise ConfigError(str(exc)) from exc

    records = []
    for c in companies:
        # A fund lists a company's own website, so the URL is declared rather
        # than cited as evidence. The aggregator screen must not run here: it
        # exists for landscape files where the url may be an arXiv abstract, and
        # applying it would throw away Medium and Substack for being on the
        # blogging-platform list.
        domain = declared_domain(c.domain_url)
        confidence = "declared" if domain else "unknown"
        if not domain:
            report.no_domain.append(c.name)
        records.append(CompanyRecord(
            name=c.name,
            domain=domain,
            domain_confidence=confidence,
            what=(c.description or None),
            evidence_url=c.domain_url or c.detail_url,
            source="vc",
            source_ref=f"{fund_name}:{spec['url']}",
            fund=fund_name,
            stages=c.stages,
            verticals=c.verticals,
            year_founded=c.year_founded,
            founders=c.founders,
        ))
    report.founders_found = sum(len(c.founders) for c in companies)
    return records, report


# ------------------------------------------------------------------ persist


def _next_status(existing: str | None, excluded: bool, degraded: bool) -> str:
    """Decide an account's status on re-import.

    Exclusion is sticky. Someone already decided against this company, and a
    later run that happens to list it should not quietly put it back in the
    queue -- clearing an exclusion is a human's call.

    Research progress is also preserved: a company that is already researched
    must not be demoted to `new` because a second run mentioned it.
    """
    if excluded or existing == "excluded":
        return "excluded"
    if existing in ("done", "researching"):
        return existing
    return "degraded" if degraded else "new"


def upsert(conn: sqlite3.Connection, records: list[CompanyRecord],
           report: DiscoveryReport, degraded: bool = False) -> None:
    for r in records:
        key = normalize_company(r.name)
        if not key:
            continue
        row = conn.execute(
            "SELECT id, status, domain FROM accounts WHERE name_normalized = ?", (key,)
        ).fetchone()
        status = _next_status(row["status"] if row else None,
                              bool(r.excluded_reason), degraded)

        if row:
            conn.execute(
                "UPDATE accounts SET tier = COALESCE(?, tier), campaign = COALESCE(?, campaign),"
                " domain = COALESCE(domain, ?),"
                " domain_confidence = CASE WHEN domain IS NULL AND ? IS NOT NULL THEN ?"
                "   ELSE domain_confidence END,"
                " what = COALESCE(?, what), entry_note = COALESCE(?, entry_note),"
                " ships = COALESCE(?, ships), subproblems = COALESCE(?, subproblems),"
                " evidence_url = COALESCE(?, evidence_url),"
                " excluded_reason = COALESCE(?, excluded_reason),"
                " source_note = COALESCE(?, source_note), fund = COALESCE(?, fund),"
                " stages = COALESCE(?, stages), verticals = COALESCE(?, verticals),"
                " year_founded = COALESCE(?, year_founded),"
                " status = ?, updated_at = ? WHERE id = ?",
                (r.tier, r.campaign, r.domain, r.domain, r.domain_confidence,
                 r.what, r.entry_note,
                 int(r.ships) if r.ships is not None else None,
                 ",".join(r.subproblems) or None, r.evidence_url, r.excluded_reason,
                 r.source_note, r.fund, r.stages, r.verticals, r.year_founded,
                 status, utcnow(), row["id"]),
            )
            if status == "excluded":
                report.excluded += 1
            else:
                report.updated += 1
        else:
            conn.execute(
                "INSERT INTO accounts (name, name_normalized, domain, source, source_ref,"
                " status, excluded_reason, tier, campaign, what, entry_note, ships,"
                " subproblems, evidence_url, domain_confidence, source_note, fund,"
                " stages, verticals, year_founded, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.name, key, r.domain, r.source, r.source_ref, status, r.excluded_reason,
                 r.tier, r.campaign, r.what, r.entry_note,
                 int(r.ships) if r.ships is not None else None,
                 ",".join(r.subproblems) or None, r.evidence_url, r.domain_confidence,
                 r.source_note, r.fund, r.stages, r.verticals, r.year_founded,
                 utcnow(), utcnow()),
            )
            if status == "excluded":
                report.excluded += 1
            else:
                report.added += 1

        if r.founders:
            account_id = conn.execute(
                "SELECT id FROM accounts WHERE name_normalized = ?", (key,)
            ).fetchone()["id"]
            for person in r.founders:
                conn.execute(
                    "INSERT OR IGNORE INTO known_people (account_id, name, role,"
                    " provenance, source_url, created_at) VALUES (?,?,?,?,?,?)",
                    (account_id, person, "founder", "fund_portfolio",
                     r.evidence_url, utcnow()),
                )

    log_event(conn, "info", "discover.import", source=report.source,
              ref=report.source_ref, added=report.added, updated=report.updated)


# ------------------------------------------------------------------ cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover companies into the accounts table.")
    ap.add_argument("--mode", choices=["list", "vc", "industry"], required=True)
    ap.add_argument("--run", help="industry-research run directory (mode=industry)")
    ap.add_argument("--fund", help="fund name from funds.yaml (mode=vc)")
    ap.add_argument("--file", help="company list file (mode=list|vc)")
    ap.add_argument("--force-refetch", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tier", help="tier to assign (mode=list|vc)")
    ap.add_argument("--config")
    ap.add_argument("--db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    tiers = {t for c in config.campaigns.campaigns.values() for t in c.tiers}

    try:
        if args.mode == "industry":
            if not args.run:
                print("--run is required for mode=industry", file=sys.stderr)
                return 2
            records, report = from_industry_run(config, Path(args.run), tiers)
        elif args.mode == "vc" and args.fund:
            records, report = from_fund(config, args.fund, force=args.force_refetch,
                                        limit=args.limit)
        else:
            if not args.file:
                print("--file or --fund is required for this mode", file=sys.stderr)
                return 2
            records, report = from_name_list(Path(args.file), args.mode, config, args.tier)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    conn = open_db(args.db)
    if args.dry_run:
        print(f"dry run -- {len(records)} record(s) parsed, nothing written")
        for r in records[:15]:
            print(f"  {r.tier or '-':<14} {r.domain or '(no domain)':<28} {r.name}")
        return 0

    with transaction(conn):
        upsert(conn, records, report, degraded=bool(report.degraded_run))
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
