"""Suppression is permanent, global, and checked everywhere including discovery."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.db import get_or_create_campaign, utcnow
from scripts.ingest_candidates import ingest
from scripts.normalize import normalize_company
from scripts.suppression import (
    add,
    filter_addresses,
    is_suppressed,
    load_csv,
    load_set,
    suppress_company,
)


@pytest.fixture
def candidates(tmp_path, candidates_dir) -> Path:
    dst = tmp_path / "candidates"
    shutil.copytree(candidates_dir, dst)
    return dst


def test_add_is_idempotent(conn):
    assert add(conn, "email", "a@b.test", "opted out") is True
    assert add(conn, "email", "a@b.test", "opted out again") is False


def test_email_domain_and_company_all_match(conn):
    add(conn, "email", "a@b.test", "r1")
    add(conn, "domain", "c.test", "r2")
    add(conn, "company", "Kepler Systems, Inc.", "r3")
    assert is_suppressed(conn, "a@b.test")
    assert is_suppressed(conn, "someone@c.test")
    assert is_suppressed(conn, "x@y.test", company="Kepler Systems")


def test_company_key_ignores_legal_suffix(conn):
    add(conn, "company", "Kepler Systems, Inc.", "reply")
    assert is_suppressed(conn, "x@y.test", company="Kepler Systems LLC")


def test_unknown_kind_is_rejected(conn):
    with pytest.raises(ValueError, match="unknown suppression kind"):
        add(conn, "persno", "a@b.test", "typo")


def test_csv_round_trip_survives_losing_the_database(conn, tmp_path):
    csv_path = tmp_path / "suppression.csv"
    add(conn, "email", "gone@b.test", "unsubscribed", csv_path=csv_path)
    assert csv_path.exists()

    from scripts.db import connect, migrate
    fresh = connect(tmp_path / "fresh.db")
    migrate(fresh)
    assert load_csv(fresh, csv_path) == 1
    assert is_suppressed(fresh, "gone@b.test")


def test_filter_addresses_splits_allowed_from_suppressed(conn):
    add(conn, "email", "no@b.test", "opted out")
    allowed, blocked = filter_addresses(conn, ["yes@b.test", "no@b.test"])
    assert allowed == ["yes@b.test"]
    assert blocked == ["no@b.test"]


def test_reply_from_one_contact_stops_the_whole_company(conn, config, candidates):
    """Otherwise you email four people at a startup after one already said no."""
    ingest(conn, config, candidates)
    campaign_id = get_or_create_campaign(conn, "default")
    contacts = conn.execute(
        "SELECT c.id, c.email FROM contacts c JOIN accounts a ON a.id = c.account_id"
        " WHERE a.name_normalized = 'northwind'"
    ).fetchall()
    assert len(contacts) == 2

    for c in contacts:
        conn.execute(
            "INSERT INTO enrollments (contact_id, campaign_id, current_step, next_send_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (c["id"], campaign_id, "step1_initial", utcnow(), utcnow(), utcnow()),
        )
        conn.execute(
            "INSERT INTO messages (contact_id, campaign_id, step_id, mailbox_id, state,"
            " to_addr, subject, body_hash, template_hash, idempotency_key)"
            " VALUES (?,?,?,?,'queued',?,?,?,?,?)",
            (c["id"], campaign_id, "step2_bump", "console", c["email"], "s", "b", "t",
             f"{c['id']}:step2_bump"),
        )

    # One person replies.
    suppress_company(conn, "Northwind Labs", "replied: not interested")

    stopped = conn.execute(
        "SELECT COUNT(*) FROM enrollments WHERE stopped = 1"
    ).fetchone()[0]
    assert stopped == 2, "both contacts at the company must stop, not just the replier"

    cancelled = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE state = 'cancelled'"
    ).fetchone()[0]
    assert cancelled == 2, "queued-but-unsent mail to that company must not go out"

    assert is_suppressed(conn, "someone-new@northwindlabs.test", company="Northwind Labs")


def test_company_suppression_leaves_other_companies_alone(conn, config, candidates):
    ingest(conn, config, candidates)
    suppress_company(conn, "Northwind Labs", "replied")
    kepler = conn.execute(
        "SELECT status FROM contacts WHERE email = 'alan@keplersystems.test'"
    ).fetchone()
    assert kepler["status"] == "new"


def test_load_set_returns_every_value(conn):
    add(conn, "email", "a@b.test", "r")
    add(conn, "domain", "c.test", "r")
    assert load_set(conn) == {"a@b.test", "c.test"}
