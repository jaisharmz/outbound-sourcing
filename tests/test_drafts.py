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


def test_role_addresses_are_not_people():
    """The pitch opens "Hello <first name>" and quotes the recipient's own work,
    so a shared inbox is the wrong destination even when it is deliverable.
    Found live on a Groq engineer's site as web@zvfh.dev."""
    from scripts.person_pages import emails_on

    assert emails_on("", "web@zvfh.dev or ashay@zvfh.dev") == ["ashay@zvfh.dev"]
    assert emails_on("", "info@lab.edu and careers@lab.edu") == []


def test_brace_form_addresses_are_expanded():
    """Academic first pages compress shared domains as {a,b,c}@company.com. A
    reader that only understands plain addresses finds nothing on exactly the
    papers most likely to list a whole team."""
    from scripts.paper_emails import emails_in

    t = "Authors {alice,bob carol}@groq.com, also dave@groq.com, cited eve@mit.edu"
    assert emails_in(t, "groq.com") == ["alice@groq.com", "bob@groq.com",
                                        "carol@groq.com", "dave@groq.com"]
    # The brace group must be stripped before the plain pass, or "b}@x" survives.
    assert all("}" not in e for e in emails_in(t))


def test_an_address_matching_no_author_is_left_unattributed():
    """An address without an identity is the thing this channel exists to avoid,
    so it is never guessed onto the nearest name."""
    from scripts.paper_emails import PaperHit, pair

    hit = PaperHit("1234", "T", ["dabts@groq.com", "mystery@groq.com"],
                   ["Dennis Abts", "Someone Else"])
    att, counts = pair(hit)
    assert att == {"dabts@groq.com": "Dennis Abts"}
    assert counts == {"first_initial_last": 1}


def test_old_papers_do_not_prove_current_employment():
    """The 2022 Groq TSP paper yielded eight @groq.com addresses; at least two
    of those authors have since moved to NVIDIA. Pitching them "your team at
    Groq" would be wrong about the one fact the email asserts. Same defect as a
    commit email, found the same way."""
    from scripts.paper_emails import paper_year_month

    assert paper_year_month("2206.11062v1") == (2022, 6)
    assert paper_year_month("2407.03651v2") == (2024, 7)
    assert paper_year_month("nonsense") is None


def test_a_rewritten_from_is_detected(monkeypatch):
    """Gmail silently rewrites From to the authenticated account unless the
    address is a verified alias. The sent copy shows what was asked for and the
    delivered copy shows what the recipient sees, so the mismatch is visible
    only by comparing them -- and it changes who the recipient thinks is
    writing."""
    from email.utils import parseaddr

    sent = "Jai Sharma <jais@berkeley.edu>"
    delivered = ("From: Jai Sharma <jaisharmaus@gmail.com>\n"
                 "Reply-To: jais@berkeley.edu\n")
    want = parseaddr(sent)[1].lower()
    got = next(parseaddr(l.partition(":")[2])[1].lower()
               for l in delivered.splitlines() if l.lower().startswith("from:"))
    assert want != got, "this fixture exists because the rewrite really happens"

    ok_delivered = "From: Jai Sharma <jais@berkeley.edu>\n"
    got_ok = next(parseaddr(l.partition(":")[2])[1].lower()
                  for l in ok_delivered.splitlines() if l.lower().startswith("from:"))
    assert want == got_ok


def test_reply_to_survives_a_from_rewrite():
    """Replies are the metric. Even with From rewritten, Reply-To is what a
    client uses when the recipient hits reply, so the reply path is intact
    while the display name is not."""
    from pathlib import Path

    from scripts.config import Config

    # Reply-To is the load-bearing half: Gmail rewrites From when the address is
    # not a verified alias, but never touches Reply-To. The From address itself
    # is an operator choice and is deliberately not pinned here.
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    mb = cfg.mailboxes.get("gmail-smtp")
    assert mb.reply_to and "@" in mb.reply_to
    assert mb.from_.address and "@" in mb.from_.address


def test_the_send_gate_blocks_a_rewritten_from(tmp_path, monkeypatch):
    """Sending messages that claim a university address and arrive from a
    personal gmail is worse than not sending: it is the exact mismatch a
    recipient reads as a spoof, and it cannot be taken back."""
    from pathlib import Path

    from scripts.config import Config
    from scripts.db import open_db
    from scripts.send_queue import gate

    conn = open_db(str(tmp_path / "g.db"))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    # Derived from config, not hardcoded: the configured From is an operator
    # choice that changes, and a test that pins it fails for the wrong reason.
    configured = cfg.mailboxes.get("gmail-smtp").from_.address
    conn.execute(
        "INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash,"
        " to_addr, ok, headers, sent_at) VALUES ('gmail-smtp','step1_initial',"
        " 'startup','h','x@y.test',1,?,'')",
        (f"From: Jai Sharma <{configured}>\n\n--- delivered ---\n"
         "From: Someone Else <rewritten@elsewhere.test>\n",))
    conn.commit()
    problems = gate(conn, cfg, "startup", "gmail-smtp")
    assert any("delivered as rewritten@elsewhere.test" in p for p in problems)
    assert any("Send mail as" in p for p in problems)


def test_the_gate_passes_when_from_survives(tmp_path):
    from pathlib import Path

    from scripts.config import Config
    from scripts.db import open_db
    from scripts.send_queue import gate

    conn = open_db(str(tmp_path / "g2.db"))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    configured = cfg.mailboxes.get("gmail-smtp").from_.address
    conn.execute(
        "INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash,"
        " to_addr, ok, headers, sent_at) VALUES ('gmail-smtp','step1_initial',"
        " 'startup','h','x@y.test',1,?,'')",
        (f"From: Jai Sharma <{configured}>\n\n--- delivered ---\n"
         f"From: Jai Sharma <{configured}>\n",))
    conn.commit()
    assert not any("delivered as" in p for p in gate(conn, cfg, "startup", "gmail-smtp"))
