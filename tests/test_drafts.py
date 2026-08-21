"""A draft is not a send.

The whole point of the drafted state is that it does not lie about contact. If
drafting marked someone contacted, a batch the operator prepared and then
decided against would suppress those companies forever and start a reply clock
on mail that never left.
"""

from __future__ import annotations

import pytest

from scripts.providers import SendResult


def test_drafted_is_a_distinct_state(conn):
    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at) VALUES (1,'Acme','acme','t','active','','')")
    conn.execute("INSERT INTO contacts (id, account_id, name, first_name, last_name, title,"
                 " email, email_domain, email_basis, confidence, created_at, updated_at)"
                 " VALUES (1,1,'A B','A','B','R','a@b.test','b.test','observed',0.9,'','')")
    conn.execute("INSERT INTO messages (contact_id, step_id, mailbox_id, state, to_addr)"
                 " VALUES (1,'s','m','drafted','a@b.test')")
    with pytest.raises(Exception):
        conn.execute("INSERT INTO messages (contact_id, step_id, mailbox_id, state, to_addr)"
                     " VALUES (1,'s','m','not_a_state','a@b.test')")


def test_a_draft_does_not_count_as_contact(monkeypatch, tmp_path):
    """Cap untouched, contact still 'new', nothing for reply tracking to key on."""
    from scripts import send_queue

    calls = {}

    class FakeProvider:
        def create_draft(self, email):
            calls["drafted"] = email.to
            return SendResult(ok=True, message_id="<draft@test>")

        def send(self, email):
            calls["sent"] = email.to
            return SendResult(ok=True, message_id="<sent@test>")

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    ok, detail = send_queue.send_one(conn, config, FakeProvider(), mailbox, row,
                                     "startup", 1, dry_run=False, mode="draft")
    assert ok and "drafted for" in detail
    assert calls.get("drafted") and "sent" not in calls

    msg = conn.execute("SELECT state, drafted_at, sent_at FROM messages").fetchone()
    assert msg["state"] == "drafted" and msg["drafted_at"] and msg["sent_at"] is None
    assert conn.execute("SELECT status FROM contacts WHERE id=?",
                        (row["id"],)).fetchone()["status"] == "new"
    assert conn.execute("SELECT COUNT(*) FROM mailbox_day").fetchone()[0] == 0


def test_send_mode_still_sends(monkeypatch, tmp_path):
    from scripts import send_queue

    calls = {}

    class FakeProvider:
        def create_draft(self, email):
            calls["drafted"] = True
            return SendResult(ok=True, message_id="<d@test>")

        def send(self, email):
            calls["sent"] = True
            return SendResult(ok=True, message_id="<s@test>")

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    ok, _ = send_queue.send_one(conn, config, FakeProvider(), mailbox, row,
                                "startup", 1, dry_run=False, mode="send")
    assert ok and calls.get("sent") and "drafted" not in calls
    assert conn.execute("SELECT state FROM messages").fetchone()["state"] == "sent"
    assert conn.execute("SELECT status FROM contacts WHERE id=?",
                        (row["id"],)).fetchone()["status"] == "active"
    assert conn.execute("SELECT COUNT(*) FROM mailbox_day").fetchone()[0] == 1


def test_a_provider_without_drafts_says_so_rather_than_sending():
    """The dangerous failure would be falling back to send. It returns an error."""
    from scripts.providers import MailboxProvider

    class Bare(MailboxProvider):
        def send(self, email): return SendResult(ok=True)
        def list_replies(self, thread_ids, since=None): return []

    r = Bare.create_draft(Bare.__new__(Bare), object())
    assert not r.ok and "cannot create drafts" in r.error


def _fixture(tmp_path, monkeypatch):
    """One approved contact in a scratch db, with rendering stubbed."""
    from scripts.db import open_db
    from scripts.config import Config
    from pathlib import Path
    from scripts import send_queue

    conn = open_db(str(tmp_path / "d.db"))
    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at) VALUES (1,'Acme','acme','t','active','','')")
    conn.execute("INSERT INTO contacts (id, account_id, name, first_name, last_name, title,"
                 " email, email_domain, email_basis, confidence, status, sendable, approved,"
                 " verification_status, created_at, updated_at)"
                 " VALUES (1,1,'A B','A','B','Researcher','a@b.test','b.test','observed',"
                 " 0.9,'new',1,1,'mx_only','','')")
    conn.commit()
    config = Config(Path(__file__).resolve().parent.parent / "config")
    mailbox = config.mailboxes.get("gmail-smtp")
    row = conn.execute("SELECT c.*, a.name AS account_name, a.domain AS account_domain"
                       " FROM contacts c JOIN accounts a ON a.id=c.account_id"
                       " WHERE c.id=1").fetchone()
    monkeypatch.setattr(send_queue.suppression, "is_suppressed", lambda *a, **k: None)
    return row, conn, config, mailbox


def test_spam_traps_are_never_harvested():
    """A page in the Together AI run carried hate@spam.net next to a real
    address -- a trap planted to catch scrapers. Harvesting one is bad; sending
    to one is how a sending domain lands on a blocklist, and that damages every
    later message rather than just the one."""
    from scripts.person_pages import emails_on

    found = emails_on("", "reach me at hate@spam.net or real@together.ai")
    assert found == ["real@together.ai"]
    assert emails_on("", "noreply@x.test and postmaster@x.test") == []
