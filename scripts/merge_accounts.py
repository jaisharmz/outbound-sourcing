"""Merge one account into another when the roster contains both sides of a deal.

A fund portfolio is a snapshot. Across 667 companies it will contain pairs where
one has since acquired the other -- Flock and Aerodome were the first found, and
they will not be the last. Queueing both means two emails into one company, one
of them addressed to a business unit that no longer exists under that name.

The acquired row is kept, not deleted: its people, its evidence and its history
stay attached and reachable, and the merge records why.
"""

from __future__ import annotations

import sqlite3

from .db import log_event, utcnow
from .normalize import normalize_company


class MergeError(RuntimeError):
    pass


def merge(conn: sqlite3.Connection, *, acquired: str, acquirer: str,
          reason: str, source_url: str | None = None) -> dict[str, int]:
    """Fold `acquired` into `acquirer`. Idempotent."""
    src = conn.execute("SELECT id, name, status FROM accounts WHERE name_normalized = ?",
                       (normalize_company(acquired),)).fetchone()
    dst = conn.execute("SELECT id, name FROM accounts WHERE name_normalized = ?",
                       (normalize_company(acquirer),)).fetchone()
    if not src:
        raise MergeError(f"no account named {acquired!r}")
    if not dst:
        raise MergeError(f"no account named {acquirer!r}")
    if src["id"] == dst["id"]:
        raise MergeError("cannot merge an account into itself")

    note = f"acquired by {dst['name']}: {reason}"
    moved_people = conn.execute(
        "UPDATE known_people SET account_id = ? WHERE account_id = ?"
        " AND name NOT IN (SELECT name FROM known_people WHERE account_id = ?)",
        (dst["id"], src["id"], dst["id"]),
    ).rowcount
    moved_contacts = conn.execute(
        "UPDATE contacts SET account_id = ? WHERE account_id = ?", (dst["id"], src["id"])
    ).rowcount
    # Anything left is a duplicate person already known at the acquirer.
    dropped = conn.execute(
        "DELETE FROM known_people WHERE account_id = ?", (src["id"],)
    ).rowcount

    conn.execute(
        "UPDATE accounts SET status = 'merged', merged_into_id = ?, merged_at = ?,"
        " merge_reason = ?, excluded_reason = COALESCE(excluded_reason, ?),"
        " liveness_status = 'acquired', liveness_note = ?, liveness_source = ?,"
        " updated_at = ? WHERE id = ?",
        (dst["id"], utcnow(), note, note, note, source_url, utcnow(), src["id"]),
    )
    log_event(conn, "info", "accounts.merge", acquired=src["name"], acquirer=dst["name"],
              people=moved_people, contacts=moved_contacts)
    return {"people_moved": moved_people, "contacts_moved": moved_contacts,
            "duplicate_people_dropped": dropped}


def queueable(conn: sqlite3.Connection) -> int:
    """Accounts that may enter the queue. Merged rows never can."""
    return conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE status NOT IN"
        " ('excluded','merged','excluded_region')"
    ).fetchone()[0]


def exclude_region(conn: sqlite3.Connection, name: str, region: str,
                   source: str, reason: str) -> None:
    """Drop for region, visibly. The rule is the default; the cost is on record."""
    conn.execute(
        "UPDATE accounts SET status = 'excluded_region', region = ?, region_source = ?,"
        " excluded_reason = ?, updated_at = ? WHERE name_normalized = ?",
        (region, source, reason, utcnow(), normalize_company(name)),
    )
    log_event(conn, "info", "accounts.exclude_region", company=name, region=region)
