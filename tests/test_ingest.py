"""Ingestion is the only door between the agentic layer and the deterministic one."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.ingest_candidates import ingest
from scripts.suppression import add, load_set


@pytest.fixture
def candidates(tmp_path, candidates_dir) -> Path:
    dst = tmp_path / "candidates"
    shutil.copytree(candidates_dir, dst)
    return dst


def test_ingests_the_fixture_set(conn, config, candidates):
    report = ingest(conn, config, candidates)
    assert report.files_ok == 3
    assert report.files_rejected == 0
    assert report.contacts_added == 3
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 6


def test_every_ingested_contact_keeps_its_evidence(conn, config, candidates):
    ingest(conn, config, candidates)
    rows = conn.execute(
        "SELECT c.email, COUNT(e.id) AS n FROM contacts c"
        " LEFT JOIN evidence e ON e.contact_id = c.id GROUP BY c.id"
    ).fetchall()
    assert rows and all(r["n"] >= 2 for r in rows)
    assert all(r["url"].startswith("http") for r in conn.execute("SELECT url FROM evidence"))


def test_budget_exhausted_company_is_marked_degraded_not_done(conn, config, candidates):
    """A subagent that ran out of search budget produced a thin answer, not a
    finished one. It must re-queue rather than look complete."""
    report = ingest(conn, config, candidates)
    assert "Kepler Systems, Inc." in report.degraded_companies
    status = conn.execute(
        "SELECT status FROM accounts WHERE name_normalized = 'kepler systems'"
    ).fetchone()["status"]
    assert status == "degraded"


def test_clean_company_is_done(conn, config, candidates):
    ingest(conn, config, candidates)
    status = conn.execute(
        "SELECT status FROM accounts WHERE name_normalized = 'northwind'"
    ).fetchone()["status"]
    assert status == "done"


def test_empty_company_is_recorded_with_its_reason(conn, config, candidates):
    report = ingest(conn, config, candidates)
    assert report.files_ok == 3
    row = conn.execute(
        "SELECT * FROM accounts WHERE name_normalized = 'silent'"
    ).fetchone()
    assert row is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE account_id = ?", (row["id"],)
    ).fetchone()[0] == 0


def test_reingest_is_idempotent(conn, config, candidates):
    ingest(conn, config, candidates)
    second = ingest(conn, config, candidates)
    assert second.contacts_added == 0
    assert second.contacts_updated == 3
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 3
    # Evidence is replaced, not appended, on re-ingest.
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 6


def test_rejected_file_does_not_partially_ingest(conn, config, candidates, tmp_path):
    bad = json.loads((candidates / "northwind-labs.json").read_text())
    bad["company"] = "Broken Co"
    bad["candidates"][0]["evidence"][1]["url"] = "not-a-url"
    (candidates / "broken.json").write_text(json.dumps(bad))

    report = ingest(conn, config, candidates)
    assert report.files_rejected == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE name_normalized = 'broken'"
    ).fetchone()[0] == 0


def test_off_icp_titles_are_dropped(conn, config, candidates):
    rec = json.loads((candidates / "northwind-labs.json").read_text())
    rec["company"] = "Recruit Co"
    c = rec["candidates"][0]
    c["title"] = "Technical Recruiter"
    c["company"] = "Recruit Co"
    c["email"] = "rec@recruitco.test"
    c["evidence"][0]["claim"] = "Ada Lovelace works at Recruit Co as a Technical Recruiter"
    c["evidence"][1]["claim"] = "email is rec@recruitco.test"
    c["evidence"][1]["quote"] = "Ada Lovelace (rec@recruitco.test)"
    rec["candidates"] = [c]
    (candidates / "recruit.json").write_text(json.dumps(rec))

    report = ingest(conn, config, candidates)
    assert any("rec@recruitco.test" in d for d in report.dropped_icp)
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE email = 'rec@recruitco.test'"
    ).fetchone()[0] == 0


def _free_mail_candidate(candidates, basis):
    rec = json.loads((candidates / "kepler-systems.json").read_text())
    c = rec["candidates"][0]
    c["email"] = "alan.turing@gmail.com"
    c["email_basis"] = basis
    c["evidence"][1]["claim"] = "email is alan.turing@gmail.com"
    c["evidence"][1]["quote"] = "Author: Alan Turing <alan.turing@gmail.com>"
    rec["candidates"] = [c]
    (candidates / "kepler-systems.json").write_text(json.dumps(rec))


def test_inferred_free_mail_is_dropped(conn, config, candidates):
    """There is no pattern to infer from at gmail.com, so an inferred consumer
    address is a guess wearing the shape of a record."""
    _free_mail_candidate(candidates, "inferred_from_pattern")
    report = ingest(conn, config, candidates)
    assert any("alanturing@gmail.com" in d for d in report.dropped_free_mail)


def test_observed_free_mail_is_kept(conn, config, candidates):
    """An address the person published on their own homepage as their contact is
    first-party and current -- it is the route they chose to be reached by.
    Dropping these took Hugging Face from 15 addresses to 1."""
    _free_mail_candidate(candidates, "observed")
    report = ingest(conn, config, candidates)
    assert not any("alanturing@gmail.com" in d for d in report.dropped_free_mail)
    assert conn.execute("SELECT COUNT(*) FROM contacts WHERE email=?",
                        ("alanturing@gmail.com",)).fetchone()[0] == 1


def test_max_contacts_per_company_is_enforced(conn, config, candidates):
    """The cap limits how many are SENDABLE, not how many are kept.

    Over-cap records stay in the database marked unsendable so a colleague can
    work them by hand; send_queue.due() filters on sendable=1, so they can never
    be drafted. Discarding them threw away evidence that had already been paid for.
    """
    config.icp.max_contacts_per_company = 1
    report = ingest(conn, config, candidates)
    sendable = conn.execute(
        "SELECT COUNT(*) FROM contacts c JOIN accounts a ON a.id = c.account_id"
        " WHERE a.name_normalized = 'northwind' AND c.sendable = 1"
    ).fetchone()[0]
    assert sendable == 1
    benched = conn.execute(
        "SELECT COUNT(*) FROM contacts c JOIN accounts a ON a.id = c.account_id"
        " WHERE a.name_normalized = 'northwind' AND c.sendable = 0"
    ).fetchone()[0]
    assert benched >= 1
    assert len(report.benched) == benched


def test_suppression_survives_a_fresh_discovery_run(conn, config, candidates):
    """A person who opted out never resurfaces, even from brand-new discovery output."""
    add(conn, "email", "ada@northwindlabs.test", "unsubscribed")
    report = ingest(conn, config, candidates)
    assert any("ada@northwindlabs.test" in s for s in report.dropped_suppressed)
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE email = 'ada@northwindlabs.test'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 2


def test_company_suppression_blocks_every_contact_there(conn, config, candidates):
    add(conn, "company", "Northwind Labs", "asked us to stop")
    ingest(conn, config, candidates)
    n = conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE email_domain = 'northwindlabs.test'"
    ).fetchone()[0]
    assert n == 0


def test_domain_suppression_blocks_the_whole_domain(conn, config, candidates):
    add(conn, "domain", "northwindlabs.test", "bounced hard")
    ingest(conn, config, candidates)
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE email_domain = 'northwindlabs.test'"
    ).fetchone()[0] == 0


def test_dry_run_writes_nothing(conn, config, candidates):
    report = ingest(conn, config, candidates, dry_run=True)
    assert report.files_ok == 3
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0


def test_contacts_inherit_segmentation_from_their_account(conn, config, candidates):
    """Tier, campaign and depth have to reach the contact, or reporting blends
    campaigns that fail for different reasons."""
    ingest(conn, config, candidates)
    conn.execute("UPDATE accounts SET tier='startup', campaign='startup', ai_depth='builds'")
    ingest(conn, config, candidates)
    rows = conn.execute("SELECT tier, campaign, ai_depth FROM contacts").fetchall()
    assert rows and all(
        (r["tier"], r["campaign"], r["ai_depth"]) == ("startup", "startup", "builds")
        for r in rows
    )


def test_a_researched_but_empty_company_is_not_done(conn, config, candidates):
    """`no_contacts` is distinct from `done`: nobody found is not finished, it is
    waiting on something to change, and `done` hides it from every re-queue."""
    ingest(conn, config, candidates)
    assert conn.execute(
        "SELECT status FROM accounts WHERE name_normalized = 'silent'"
    ).fetchone()["status"] == "no_contacts"
    assert conn.execute(
        "SELECT status FROM accounts WHERE name_normalized = 'northwind'"
    ).fetchone()["status"] == "done"


def test_known_people_resolve_once_an_address_lands(conn, config, candidates):
    """A later brief must not spend budget rediscovering someone we can now email."""
    from scripts.db import utcnow
    ingest(conn, config, candidates)
    acct = conn.execute(
        "SELECT id FROM accounts WHERE name_normalized = 'northwind'").fetchone()["id"]
    conn.execute(
        "INSERT INTO known_people (account_id, name, role, provenance, created_at)"
        " VALUES (?,?,?,?,?)", (acct, "Ada Lovelace", "founder", "fund_portfolio", utcnow()))
    ingest(conn, config, candidates)
    assert conn.execute(
        "SELECT resolved FROM known_people WHERE name = 'Ada Lovelace'").fetchone()[0] == 1
