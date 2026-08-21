"""Load validated candidate files into SQLite.

This is the only door between the agentic layer and the deterministic one.
A file that fails validation is not partially ingested -- the whole company is
rejected and reported, because a record that got halfway in is worse than one
that never arrived.

    python -m scripts.ingest_candidates [--dir state/candidates] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .candidates import Candidate, CandidateError, CandidateFile, filter_suppressed, validate_file
from .config import Config, load_config
from .db import log_event, open_db, transaction, utcnow
from .errors import ConfigError
from .normalize import (
    domain_of,
    is_free_mail,
    normalize_company,
    normalize_email,
    normalize_person,
    registrable_domain,
)
from . import exclusions, leadership, seniority
from .suppression import is_suppressed, lab_is_full, load_set


@dataclass
class IngestReport:
    files_seen: int = 0
    files_ok: int = 0
    files_rejected: int = 0
    contacts_added: int = 0
    contacts_updated: int = 0
    dropped_suppressed: list[str] = field(default_factory=list)
    dropped_duplicate: list[str] = field(default_factory=list)
    dropped_icp: list[str] = field(default_factory=list)
    dropped_free_mail: list[str] = field(default_factory=list)
    dropped_excluded: list[str] = field(default_factory=list)
    dropped_lab_full: list[str] = field(default_factory=list)
    dropped_leadership: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    degraded_companies: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"files:     {self.files_ok} ok, {self.files_rejected} rejected "
            f"(of {self.files_seen})",
            f"contacts:  {self.contacts_added} added, {self.contacts_updated} updated",
        ]
        for label, items in (
            ("suppressed", self.dropped_suppressed),
            ("duplicate", self.dropped_duplicate),
            ("off-ICP", self.dropped_icp),
            ("free-mail", self.dropped_free_mail),
            ("personally excluded", self.dropped_excluded),
            ("lab already at cap", self.dropped_lab_full),
            ("on a founders/leadership page", self.dropped_leadership),
        ):
            if items:
                lines.append(f"dropped {label}: {len(items)} -- {', '.join(items[:5])}"
                             + (" ..." if len(items) > 5 else ""))
        if self.degraded_companies:
            lines.append(
                f"DEGRADED: {len(self.degraded_companies)} company/companies hit the search "
                f"budget and will be re-queued: {', '.join(self.degraded_companies)}"
            )
        for err in self.errors:
            lines.append(f"REJECTED: {err}")
        return "\n".join(lines)


def passes_icp(candidate: Candidate, config: Config) -> str | None:
    """Deterministic ICP filtering against declared rules. Returns a reason to drop."""
    icp = config.icp
    title = candidate.title.lower()

    # An honestly-unknown title is not an off-ICP title. The investigation loop
    # chases a role through the roster, the team page and the person's own page;
    # when all three come back empty the record says so rather than inventing
    # one. Dropping those here would discard exactly the people the loop worked
    # hardest for, so they pass and are flagged at review for a per-row call.
    if title.strip().lower() not in ("unknown", "unknown title", ""):
        if icp.titles and not any(t.lower() in title for t in icp.titles):
            return f"title {candidate.title!r} matches no icp.titles entry"
    if any(x.lower() in title for x in icp.title_excludes):
        return f"title {candidate.title!r} matches an icp.title_excludes entry"
    if candidate.confidence < icp.min_confidence:
        return f"confidence {candidate.confidence} below icp.min_confidence {icp.min_confidence}"

    domain = registrable_domain(candidate.domain)
    if domain in {d.lower() for d in icp.exclude_domains}:
        return f"domain {domain} is in icp.exclude_domains"
    if normalize_company(candidate.company) in {normalize_company(c) for c in icp.exclude_companies}:
        return f"company {candidate.company!r} is in icp.exclude_companies"

    # GDPR: cold B2B into the EU/UK is a call the operator makes deliberately.
    country = (candidate.country or "").upper()
    if country and country in {r.upper() for r in icp.exclude_regions}:
        return f"country {country} is in icp.exclude_regions"
    tld = domain.rsplit(".", 1)[-1].upper()
    eu_tlds = {"EU", "UK", "DE", "FR", "IE", "NL", "ES", "IT", "SE", "DK", "FI", "BE", "AT", "PL", "PT"}
    if "EU" in {r.upper() for r in icp.exclude_regions} and tld in eu_tlds:
        return f"domain TLD .{tld.lower()} falls under icp.exclude_regions"

    return None


def upsert_account(conn: sqlite3.Connection, cf: CandidateFile, source: str, source_ref: str) -> int:
    key = normalize_company(cf.company)
    row = conn.execute("SELECT id FROM accounts WHERE name_normalized = ?", (key,)).fetchone()
    status = cf.status
    if row:
        conn.execute(
            "UPDATE accounts SET status = ?, searches_used = ?, budget_exhausted = ?,"
            " domain = COALESCE(?, domain), updated_at = ? WHERE id = ?",
            (status, cf.searches_used, int(cf.budget_exhausted), cf.domain, utcnow(), row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO accounts (name, name_normalized, domain, source, source_ref, status,"
        " searches_used, budget_exhausted, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cf.company, key, cf.domain, source, source_ref, status,
         cf.searches_used, int(cf.budget_exhausted), utcnow(), utcnow()),
    )
    return int(cur.lastrowid)


def upsert_contact(
    conn: sqlite3.Connection, account_id: int, c: Candidate, source_file: str
) -> tuple[int, bool]:
    # Segmentation lives on the account and has to reach the contact, or M11
    # reports a blended reply rate across campaigns that fail differently.
    acct = conn.execute(
        "SELECT tier, campaign, ai_depth FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    email = normalize_email(c.email)
    row = conn.execute("SELECT id FROM contacts WHERE email = ?", (email,)).fetchone()
    if row:
        conn.execute(
            "UPDATE contacts SET title = ?, confidence = ?, personalization = ?,"
            " personalization_source_url = ?, candidate_file = ?, tier = ?, campaign = ?,"
            " ai_depth = ?, updated_at = ? WHERE id = ?",
            (c.title, c.confidence, c.personalization, c.personalization_source_url,
             source_file, acct["tier"], acct["campaign"], acct["ai_depth"],
             utcnow(), row["id"]),
        )
        cid = int(row["id"])
        conn.execute("DELETE FROM evidence WHERE contact_id = ?", (cid,))
        _insert_evidence(conn, cid, c)
        return cid, False

    cur = conn.execute(
        "INSERT INTO contacts (account_id, name, first_name, last_name, title, email,"
        " email_domain, email_basis, confidence, personalization, personalization_source_url,"
        " country, linkedin_url, lab, candidate_file, tier, campaign, ai_depth,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, c.name, c.first_name, c.last_name, c.title, email, domain_of(email),
         c.email_basis, c.confidence, c.personalization, c.personalization_source_url,
         c.country, c.linkedin_url, c.lab, source_file,
         acct["tier"], acct["campaign"], acct["ai_depth"], utcnow(), utcnow()),
    )
    cid = int(cur.lastrowid)
    _insert_evidence(conn, cid, c)
    return cid, True


def _insert_evidence(conn: sqlite3.Connection, contact_id: int, c: Candidate) -> None:
    conn.executemany(
        "INSERT INTO evidence (contact_id, claim, url, quote, retrieved_at) VALUES (?,?,?,?,?)",
        [(contact_id, e.claim, e.url, e.quote, e.retrieved_at.isoformat()) for e in c.evidence],
    )


def ingest(
    conn: sqlite3.Connection,
    config: Config,
    directory: Path,
    *,
    source: str = "discovery",
    dry_run: bool = False,
) -> IngestReport:
    report = IngestReport()
    suppressed = load_set(conn)
    seen_people: dict[str, str] = {}   # normalized person@domain -> email already taken

    for path in sorted(Path(directory).glob("*.json")):
        report.files_seen += 1
        try:
            cf = validate_file(path)
        except CandidateError as exc:
            report.files_rejected += 1
            report.errors.append(str(exc))
            continue

        cf, dropped = filter_suppressed(cf, suppressed)
        report.dropped_suppressed.extend(dropped)
        report.files_ok += 1
        if cf.budget_exhausted:
            report.degraded_companies.append(cf.company)

        try:
            with transaction(conn):
                _ingest_company(conn, config, cf, path, source, seen_people, report)
                if dry_run:
                    # Roll this company back but keep validating the rest, so a
                    # dry run reports on the whole directory, not the first file.
                    raise _Rollback()
        except _Rollback:
            continue

    return report


def _ingest_company(
    conn: sqlite3.Connection,
    config: Config,
    cf: CandidateFile,
    path: Path,
    source: str,
    seen_people: dict[str, str],
    report: IngestReport,
) -> None:
    account_id = upsert_account(conn, cf, source, str(path))
    per_company = 0

    # One scan per company, covering every candidate in the file. A founder or
    # exec reached by a cold sequence is the failure that is only visible after
    # it has been sent, and none of the other evidence shows it: a commit and a
    # roster entry look identical whether the author writes code or runs the
    # company. Costs a handful of page fetches per company.
    on_leadership: dict[str, str] = {}
    if cf.domain and config.icp.drop_leadership:
        try:
            on_leadership = leadership.scan(cf.domain, [c.name for c in cf.candidates])
        except Exception as exc:
            report.notes.append(
                f"{cf.company}: leadership page scan failed ({type(exc).__name__}); "
                f"rows are NOT filtered for seniority")

    # Best-first, so when the per-company cap binds it drops the people least
    # likely to reply rather than whoever happened to be last in the file. The
    # ordering is the inverse of an org chart on purpose -- see scripts/seniority.
    ordered = sorted(cf.candidates,
                     key=lambda x: (seniority.rank(x.title), -(x.confidence or 0)))
    for c in ordered:
        email = normalize_email(c.email)

        lab = getattr(c, "lab", None)
        if reason := is_suppressed(conn, email, c.company, lab=lab):
            report.dropped_suppressed.append(f"{email} ({reason})")
            continue
        # Checked here rather than at send: someone the operator already knows
        # should never reach the review queue, because the reviewer's job is
        # judging the pitch, not remembering every colleague.
        if where := on_leadership.get(c.name):
            report.dropped_leadership.append(f"{c.name} ({where[:110]})")
            continue
        if ex := exclusions.check(conn, config, c.name, lab=lab, company=c.company):
            report.dropped_excluded.append(f"{c.name} <{email}> ({ex['reason']})")
            continue
        # The free-mail rule exists because an *inferred* consumer address is a
        # guess -- there is no pattern to infer from at gmail.com. An address
        # the person published on their own homepage as their contact is the
        # opposite: first-party, current, and the route they chose to be reached
        # by. Dropping those took Hugging Face from 15 addresses to 1, discarding
        # people who had explicitly said "email me here".
        if is_free_mail(domain_of(email)) and c.email_basis != "observed":
            report.dropped_free_mail.append(f"{email} (inferred, not observed)")
            continue

        person_key = f"{normalize_person(c.name)}@{registrable_domain(domain_of(email))}"
        if person_key in seen_people and seen_people[person_key] != email:
            report.dropped_duplicate.append(
                f"{email} (same person as {seen_people[person_key]})"
            )
            continue
        # Four people from one lab are colleagues who talk to each other, and
        # traversal surfaces them together. The cap keeps a single group from
        # receiving what reads as a campaign against it.
        if lab and lab_is_full(conn, lab, config.icp.max_contacts_per_lab):
            report.dropped_lab_full.append(f"{c.name} ({lab})")
            continue
        if reason := passes_icp(c, config):
            report.dropped_icp.append(f"{email} ({reason})")
            continue
        if per_company >= config.icp.max_contacts_per_company:
            report.dropped_icp.append(
                f"{email} (over max_contacts_per_company; "
                f"{seniority.name(c.title)} ranked last)")
            continue

        seen_people[person_key] = email
        _, added = upsert_contact(conn, account_id, c, str(path))
        # Mark any pre-known person as resolved so a later brief does not spend
        # budget rediscovering someone we now have an address for.
        conn.execute(
            "UPDATE known_people SET resolved = 1 WHERE account_id = ? AND name = ?",
            (account_id, c.name),
        )
        per_company += 1
        if added:
            report.contacts_added += 1
        else:
            report.contacts_updated += 1

    log_event(conn, "info", "ingest.company", company=cf.company,
              candidates=len(cf.candidates), degraded=cf.budget_exhausted)


class _Rollback(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate and load candidate files into SQLite.")
    ap.add_argument("--dir", default=None, help="candidates directory (default: from campaign.yaml)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--source", default="discovery")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent
    directory = Path(args.dir) if args.dir else root / config.campaign.discovery.candidates_dir
    conn = open_db(args.db)

    report = ingest(conn, config, directory, source=args.source, dry_run=args.dry_run)
    if args.dry_run:
        print("dry run -- nothing was written")
    print(report.summary())
    return 1 if report.files_rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
