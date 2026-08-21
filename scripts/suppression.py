"""Permanent, global, append-only suppression.

Checked at every stage including discovery, so a person who opted out never
resurfaces in a later campaign. Three kinds:

  email    one address
  domain   every address at a domain
  company  every contact at a company, by normalized name -- this is what a
           reply triggers, so one "no thanks" stops the other three people at
           the same startup
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .db import log_event, utcnow
from .normalize import normalize_company

VALID_KINDS = ("email", "domain", "company", "lab")
CSV_HEADER = ["kind", "value", "reason", "source", "created_at"]


def _key(kind: str, value: str) -> str:
    value = value.strip().lower().lstrip("@")
    return normalize_company(value) if kind == "company" else value


def add(
    conn: sqlite3.Connection,
    kind: str,
    value: str,
    reason: str,
    source: str = "manual",
    csv_path: Path | None = None,
) -> bool:
    """Suppress something. Returns False if it was already suppressed."""
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown suppression kind {kind!r}; use one of {VALID_KINDS}")
    v = _key(kind, value)
    cur = conn.execute(
        "INSERT OR IGNORE INTO suppression (kind, value, reason, source, created_at)"
        " VALUES (?,?,?,?,?)",
        (kind, v, reason, source, utcnow()),
    )
    added = cur.rowcount > 0
    if added:
        log_event(conn, "info", "suppression.add", kind=kind, value=v, reason=reason)
        if csv_path:
            append_csv(csv_path, kind, v, reason, source)
    return added


def append_csv(path: Path, kind: str, value: str, reason: str, source: str) -> None:
    """Mirror to config/suppression.csv so suppression survives losing the DB."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(CSV_HEADER)
        w.writerow([kind, value, reason, source, utcnow()])


def load_csv(conn: sqlite3.Connection, path: Path) -> int:
    """Import config/suppression.csv into the DB. Idempotent; run at startup."""
    path = Path(path)
    if not path.exists():
        return 0
    n = 0
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            kind = (row.get("kind") or "email").strip()
            value = (row.get("value") or "").strip()
            if not value or kind not in VALID_KINDS:
                continue
            if add(conn, kind, value, row.get("reason") or "imported", row.get("source") or "csv"):
                n += 1
    return n


def suppress_company(
    conn: sqlite3.Connection, company: str, reason: str, csv_path: Path | None = None
) -> None:
    """A reply stops the sequence for everyone at that company, not just the replier."""
    add(conn, "company", company, reason, source="reply", csv_path=csv_path)
    conn.execute(
        "UPDATE enrollments SET stopped = 1, stopped_reason = ?, updated_at = ?"
        " WHERE stopped = 0 AND contact_id IN ("
        "   SELECT c.id FROM contacts c JOIN accounts a ON a.id = c.account_id"
        "   WHERE a.name_normalized = ?)",
        (reason, utcnow(), normalize_company(company)),
    )
    conn.execute(
        "UPDATE contacts SET status = 'stopped', stopped_reason = ?, updated_at = ?"
        " WHERE status NOT IN ('stopped','dropped') AND account_id IN ("
        "   SELECT id FROM accounts WHERE name_normalized = ?)",
        (reason, utcnow(), normalize_company(company)),
    )
    # Queued-but-unsent mail to that company must not go out.
    conn.execute(
        "UPDATE messages SET state = 'cancelled', error = ?"
        " WHERE state = 'queued' AND contact_id IN ("
        "   SELECT c.id FROM contacts c JOIN accounts a ON a.id = c.account_id"
        "   WHERE a.name_normalized = ?)",
        (reason, normalize_company(company)),
    )
    log_event(conn, "info", "suppression.company", company=company, reason=reason)


def suppress_lab(conn: sqlite3.Connection, lab: str, reason: str,
                 csv_path: Path | None = None) -> None:
    """A reply from one person in a group stops the whole group.

    Company-level suppression misses this: four people from one university lab
    are colleagues who talk to each other, and traversal surfaces them together.
    """
    add(conn, "lab", lab, reason, source="reply", csv_path=csv_path)
    key = lab.strip().lower()
    conn.execute("UPDATE contacts SET status='stopped', stopped_reason=?, sendable=0,"
                 " unsendable_reason=?, updated_at=? WHERE LOWER(lab)=? AND status!='stopped'",
                 (reason, reason, utcnow(), key))
    conn.execute("UPDATE messages SET state='cancelled', error=? WHERE state='queued'"
                 " AND contact_id IN (SELECT id FROM contacts WHERE LOWER(lab)=?)",
                 (reason, key))
    log_event(conn, "info", "suppression.lab", lab=lab, reason=reason)


def lab_is_full(conn: sqlite3.Connection, lab: str, cap: int) -> bool:
    """True when this lab already has its share of contacts in flight."""
    if not lab:
        return False
    n = conn.execute("SELECT COUNT(*) FROM contacts WHERE LOWER(lab)=? AND sendable=1",
                     (lab.strip().lower(),)).fetchone()[0]
    return n >= cap


def load_set(conn: sqlite3.Connection) -> set[str]:
    """Every suppressed value as a flat set, for cheap membership checks."""
    return {r["value"] for r in conn.execute("SELECT value FROM suppression")}


def is_suppressed(conn: sqlite3.Connection, email: str, company: str | None = None,
                  lab: str | None = None) -> str | None:
    """Return the reason an address is suppressed, or None.

    Lab is checked alongside company because suppress_lab writing rows nobody
    reads would make lab suppression look implemented while sending anyway.
    """
    email = email.strip().lower()
    domain = email.partition("@")[2]
    checks = [("email", email), ("domain", domain)]
    if company:
        checks.append(("company", normalize_company(company)))
    if lab:
        checks.append(("lab", lab.strip().lower()))
    for kind, value in checks:
        row = conn.execute(
            "SELECT reason FROM suppression WHERE kind = ? AND value = ?", (kind, value)
        ).fetchone()
        if row:
            return f"{kind}:{value} ({row['reason']})"
    return None


def filter_addresses(conn: sqlite3.Connection, addresses: list[str]) -> tuple[list[str], list[str]]:
    """Split addresses into (allowed, suppressed). Used for CC and BCC too."""
    allowed, blocked = [], []
    for a in addresses:
        (blocked if is_suppressed(conn, a) else allowed).append(a)
    return allowed, blocked
