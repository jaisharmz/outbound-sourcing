"""Load a list of companies into the accounts table.

    python -m scripts.discover_companies --mode list --file companies.txt --tier startup

The file is one company per line, either a bare name or `name,domain`. Blank
lines and `#` comments are skipped. Re-importing is idempotent, and a domain
already known is never overwritten by a blank one.

This is the entry point for a target list you already have. Finding people
inside those companies is `outbound investigate`.
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
    relationship: str | None = None
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
                " relationship = COALESCE(?, relationship),"
                " stages = COALESCE(?, stages), verticals = COALESCE(?, verticals),"
                " year_founded = COALESCE(?, year_founded),"
                " status = ?, updated_at = ? WHERE id = ?",
                (r.tier, r.campaign, r.domain, r.domain, r.domain_confidence,
                 r.what, r.entry_note,
                 int(r.ships) if r.ships is not None else None,
                 ",".join(r.subproblems) or None, r.evidence_url, r.excluded_reason,
                 r.source_note, r.fund, r.relationship, r.stages, r.verticals,
                 r.year_founded, status, utcnow(), row["id"]),
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
                " relationship, stages, verticals, year_founded, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.name, key, r.domain, r.source, r.source_ref, status, r.excluded_reason,
                 r.tier, r.campaign, r.what, r.entry_note,
                 int(r.ships) if r.ships is not None else None,
                 ",".join(r.subproblems) or None, r.evidence_url, r.domain_confidence,
                 r.source_note, r.fund, r.relationship, r.stages, r.verticals,
                 r.year_founded, utcnow(), utcnow()),
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



# ---------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover companies into the accounts table.")
    ap.add_argument("--mode", choices=["list"], default="list")
    ap.add_argument("--file", required=True, help="company list file")
    ap.add_argument("--tier", help="tier to assign")
    ap.add_argument("--config")
    ap.add_argument("--db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
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
