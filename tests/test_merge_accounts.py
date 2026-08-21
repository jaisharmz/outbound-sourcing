"""Roster snapshots contain both sides of deals. Flock and Aerodome were the
first pair found in 667 companies and will not be the last."""

from __future__ import annotations

import pytest

from scripts.db import utcnow
from scripts.merge_accounts import MergeError, merge, queueable


def account(conn, name, status="new"):
    from scripts.normalize import normalize_company
    conn.execute("INSERT INTO accounts (name, name_normalized, source, status,"
                 " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                 (name, normalize_company(name), "vc", status, utcnow(), utcnow()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def person(conn, account_id, name):
    conn.execute("INSERT INTO known_people (account_id, name, role, provenance, created_at)"
                 " VALUES (?,?,?,?,?)", (account_id, name, "founder", "fund_portfolio", utcnow()))


def test_merge_moves_people_to_the_acquirer(conn):
    a = account(conn, "Acquirer")
    b = account(conn, "Acquired")
    person(conn, b, "Someone Real")
    res = merge(conn, acquired="Acquired", acquirer="Acquirer", reason="bought them")
    assert res["people_moved"] == 1
    assert conn.execute("SELECT account_id FROM known_people").fetchone()[0] == a


def test_the_acquired_row_is_kept_not_deleted(conn):
    """Its evidence and history stay reachable."""
    account(conn, "Acquirer")
    account(conn, "Acquired")
    merge(conn, acquired="Acquired", acquirer="Acquirer", reason="bought them")
    row = conn.execute("SELECT status, merged_into_id, merge_reason FROM accounts"
                       " WHERE name = 'Acquired'").fetchone()
    assert row["status"] == "merged"
    assert row["merged_into_id"] is not None
    assert "bought them" in row["merge_reason"]


def test_a_merged_account_can_never_be_queued(conn):
    account(conn, "Acquirer")
    account(conn, "Acquired")
    assert queueable(conn) == 2
    merge(conn, acquired="Acquired", acquirer="Acquirer", reason="bought them")
    assert queueable(conn) == 1


def test_merge_is_idempotent(conn):
    account(conn, "Acquirer")
    b = account(conn, "Acquired")
    person(conn, b, "Someone Real")
    merge(conn, acquired="Acquired", acquirer="Acquirer", reason="r")
    merge(conn, acquired="Acquired", acquirer="Acquirer", reason="r")
    assert conn.execute("SELECT COUNT(*) FROM known_people").fetchone()[0] == 1


def test_a_person_known_at_both_sides_is_not_duplicated(conn):
    a = account(conn, "Acquirer")
    b = account(conn, "Acquired")
    person(conn, a, "Shared Person")
    person(conn, b, "Shared Person")
    merge(conn, acquired="Acquired", acquirer="Acquirer", reason="r")
    assert conn.execute("SELECT COUNT(*) FROM known_people").fetchone()[0] == 1


def test_merge_refuses_unknown_or_self(conn):
    account(conn, "Only One")
    with pytest.raises(MergeError, match="no account named"):
        merge(conn, acquired="Ghost", acquirer="Only One", reason="r")
    with pytest.raises(MergeError, match="into itself"):
        merge(conn, acquired="Only One", acquirer="Only One", reason="r")


# ---------------------------------------------------------------- regions


