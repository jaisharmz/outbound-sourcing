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
    outcome, detail = send_queue.send_one(conn, config, FakeProvider(), mailbox, row,
                                          "startup", 1, dry_run=False, mode="draft")
    assert outcome == "done" and "drafted for" in detail
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
    outcome, _ = send_queue.send_one(conn, config, FakeProvider(), mailbox, row,
                                     "startup", 1, dry_run=False, mode="send")
    assert outcome == "done" and calls.get("sent") and "drafted" not in calls
    assert conn.execute("SELECT state FROM messages").fetchone()["state"] == "sent"
    assert conn.execute("SELECT status FROM contacts WHERE id=?",
                        (row["id"],)).fetchone()["status"] == "active"
    assert conn.execute("SELECT COUNT(*) FROM mailbox_day").fetchone()[0] == 1


class _Recorder:
    """A provider that remembers what it was asked to do."""

    def __init__(self, delete_ok=True):
        self.calls = []
        self.deleted = []
        self._delete_ok = delete_ok

    def create_draft(self, email):
        self.calls.append("draft")
        return SendResult(ok=True, message_id="<draft-1@test>")

    def send(self, email):
        self.calls.append("send")
        return SendResult(ok=True, message_id="<sent-1@test>")

    def delete_draft(self, message_id):
        self.deleted.append(message_id)
        return (True, "draft deleted (1)") if self._delete_ok else (False, "timed out")


def test_a_drafted_contact_can_still_be_sent(monkeypatch, tmp_path):
    """The queue is the drafts folder, so `send` has to be able to drain it.

    The idempotency key is contact:step:template_hash and is deliberately the
    same for a draft and its send. That made the second visit collide with the
    first: every one of 545 reviewed drafts would have been refused as "already
    queued, drafted or sent" and the scheduled sender would have sent nothing,
    every hour, while reporting failures.
    """
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder()

    outcome, _ = send_queue.send_one(conn, config, provider, mailbox, row,
                                     "startup", 1, dry_run=False, mode="draft")
    assert outcome == "done"

    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "done", detail
    assert provider.calls == ["draft", "send"]

    msg = conn.execute("SELECT state, drafted_at, sent_at FROM messages").fetchone()
    assert msg["state"] == "sent" and msg["sent_at"]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM contacts WHERE id=?",
                        (row["id"],)).fetchone()["status"] == "active"
    assert conn.execute("SELECT COUNT(*) FROM mailbox_day").fetchone()[0] == 1


def test_sending_a_draft_removes_it_from_the_drafts_folder(monkeypatch, tmp_path):
    """Otherwise the operator scrolls Drafts and sends the same mail by hand."""
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder()
    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="draft")
    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="send")
    assert provider.deleted == ["<draft-1@test>"]


def test_a_draft_that_will_not_delete_is_reported_not_swallowed(monkeypatch, tmp_path):
    """The send succeeded, so it is not a failure -- but a leftover draft is the
    duplicate this path exists to prevent, so it has to reach the operator."""
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder(delete_ok=False)
    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="draft")
    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "done"
    assert "draft(s) not removed" in detail
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event='draft.orphaned'"
                        ).fetchone()[0] == 1


def test_a_superseded_draft_is_cleared_too(monkeypatch, tmp_path):
    """A template edit leaves two drafts to the same person: the corrected copy
    and the stale one it was meant to replace. Both are superseded the moment
    the mail actually goes, and only the corrected one shares the send's
    idempotency key -- so matching on that key alone left 149 stale drafts in
    the folder for a human to send a second time.
    """
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder()

    # The stale draft: same contact and step, an older template hash.
    conn.execute("INSERT INTO messages (contact_id, step_id, mailbox_id, state,"
                 " to_addr, template_hash, idempotency_key, provider_message_id)"
                 " VALUES (1,'step1_initial','gmail-smtp','drafted','a@b.test',"
                 " 'oldhash','1:step1_initial:oldhash','<stale@test>')")
    conn.commit()

    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="draft")
    outcome, _ = send_queue.send_one(conn, config, provider, mailbox, row,
                                     "startup", 1, dry_run=False, mode="send")

    assert outcome == "done"
    assert sorted(provider.deleted) == ["<draft-1@test>", "<stale@test>"]
    states = dict(conn.execute("SELECT template_hash, state FROM messages"))
    assert states["oldhash"] == "cancelled"
    assert states.pop("oldhash") and set(states.values()) == {"sent"}
    # Nothing is left for `outbound drafts` to show, or for a human to click.
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE state='drafted'"
                        ).fetchone()[0] == 0


def test_an_already_sent_contact_is_held_not_resent(monkeypatch, tmp_path):
    """Promoting 'drafted' must not also promote 'sent'."""
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder()
    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="send")
    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "held" and "already sent" in detail
    assert provider.calls == ["send"]
    assert conn.execute("SELECT COUNT(*) FROM mailbox_day").fetchone()[0] == 1


def test_an_ambiguous_failure_is_not_retried_by_the_scheduler(monkeypatch, tmp_path):
    """'failed' means nobody knows whether it arrived. Only a human clears it."""
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)

    class Flaky(_Recorder):
        def send(self, email):
            self.calls.append("send")
            return SendResult(ok=False, error="TimeoutError")

    provider = Flaky()
    outcome, _ = send_queue.send_one(conn, config, provider, mailbox, row,
                                     "startup", 1, dry_run=False, mode="send")
    assert outcome == "failed"
    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "held" and "already failed" in detail
    assert provider.calls == ["send"]


def test_a_suppressed_address_is_held_rather_than_counted_as_a_failure(monkeypatch, tmp_path):
    """Anthropic was suppressed mid-campaign. An unattended hourly sender that
    counts every suppressed contact as a failure pages the operator all day
    about the queue doing exactly what it was told."""
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(send_queue.suppression, "is_suppressed",
                        lambda *a, **k: "domain suppressed")
    outcome, detail = send_queue.send_one(conn, config, _Recorder(), mailbox, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "held" and "suppressed" in detail


def test_the_dry_run_sees_what_the_real_run_would_hit(monkeypatch, tmp_path):
    """A rehearsal that returns 'ok' for a row the real run refuses is worse
    than no rehearsal: it is why a broken schedule looked ready to go."""
    from scripts import send_queue

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder()
    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="send")

    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row,
                                          "startup", 1, dry_run=True, mode="send")
    assert outcome == "held" and "already sent" in detail
    assert provider.calls == ["send"], "a dry run must not touch the provider"


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
    """A page in the Nimbus AI run carried hate@spam.net next to a real
    address -- a trap planted to catch scrapers. Harvesting one is bad; sending
    to one is how a sending domain lands on a blocklist, and that damages every
    later message rather than just the one."""
    from scripts.person_pages import emails_on

    found = emails_on("", "reach me at hate@spam.net or real@nimbus.test")
    assert found == ["real@nimbus.test"]
    assert emails_on("", "noreply@x.test and postmaster@x.test") == []


def test_role_addresses_are_not_people():
    """The pitch opens "Hello <first name>" and quotes the recipient's own work,
    so a shared inbox is the wrong destination even when it is deliverable.
    Found live on a Tensorworks engineer's site as web@personal-site.test."""
    from scripts.person_pages import emails_on

    assert emails_on("", "web@personal-site.test or ashay@personal-site.test") == ["ashay@personal-site.test"]
    assert emails_on("", "info@lab.edu and careers@lab.edu") == []


def test_brace_form_addresses_are_expanded():
    """Academic first pages compress shared domains as {a,b,c}@company.com. A
    reader that only understands plain addresses finds nothing on exactly the
    papers most likely to list a whole team."""
    from scripts.paper_emails import emails_in

    t = "Authors {alice,bob carol}@tensorworks.test, also dave@tensorworks.test, cited eve@other-university.test"
    assert emails_in(t, "tensorworks.test") == ["alice@tensorworks.test", "bob@tensorworks.test",
                                        "carol@tensorworks.test", "dave@tensorworks.test"]
    # The brace group must be stripped before the plain pass, or "b}@x" survives.
    assert all("}" not in e for e in emails_in(t))


def test_an_address_matching_no_author_is_left_unattributed():
    """An address without an identity is the thing this channel exists to avoid,
    so it is never guessed onto the nearest name."""
    from scripts.paper_emails import PaperHit, pair

    hit = PaperHit("1234", "T", ["dabts@tensorworks.test", "mystery@tensorworks.test"],
                   ["Dennis Abts", "Someone Else"])
    att, counts = pair(hit)
    assert att == {"dabts@tensorworks.test": "Dennis Abts"}
    assert counts == {"first_initial_last": 1}


def test_old_papers_do_not_prove_current_employment():
    """The 2022 Tensorworks TSP paper yielded eight @tensorworks.test addresses; at least two
    of those authors have since moved to NVIDIA. Pitching them "your team at
    Tensorworks" would be wrong about the one fact the email asserts. Same defect as a
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

    sent = "Ada Lovelace <operator@university.test>"
    delivered = ("From: Ada Lovelace <operator@gmail.com>\n"
                 "Reply-To: operator@university.test\n")
    want = parseaddr(sent)[1].lower()
    got = next(parseaddr(l.partition(":")[2])[1].lower()
               for l in delivered.splitlines() if l.lower().startswith("from:"))
    assert want != got, "this fixture exists because the rewrite really happens"

    ok_delivered = "From: Ada Lovelace <operator@university.test>\n"
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
        (f"From: Ada Lovelace <{configured}>\n\n--- delivered ---\n"
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
        (f"From: Ada Lovelace <{configured}>\n\n--- delivered ---\n"
         f"From: Ada Lovelace <{configured}>\n",))
    conn.commit()
    assert not any("delivered as" in p for p in gate(conn, cfg, "startup", "gmail-smtp"))


def test_sent_detection_needs_both_recipient_and_subject():
    """A false positive marks someone contacted who was not, which suppresses
    them and kills the follow-up. A false negative just means the next run
    reconciles it. So the match is deliberately narrow."""
    import inspect

    from scripts import reconcile

    src = inspect.getsource(reconcile.find_sent)
    assert '"TO"' in src and '"SUBJECT"' in src, "both criteria must be required"
    assert "readonly=True" in src, "reconciliation must never mutate the mailbox"


def _no_bounces(provider, since):
    """A clean mailbox: nothing Gmail accepted came back."""
    from scripts.bounces import BounceReport

    return BounceReport()


def test_reconcile_marks_only_what_it_matched(tmp_path, monkeypatch):
    from pathlib import Path

    from scripts import reconcile
    from scripts.config import Config
    from scripts.db import open_db

    conn = open_db(str(tmp_path / "r.db"))
    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at)"
                 " VALUES (1,'Acme','acme','t','active','','')")
    for i, addr in enumerate(("gone@acme.test", "waiting@acme.test"), start=1):
        conn.execute("INSERT INTO contacts (id, account_id, name, first_name, last_name,"
                     " title, email, email_domain, email_basis, confidence,"
                     " created_at, updated_at) VALUES (?,1,?,?,'X','R',?,'acme.test',"
                     "'observed',0.9,'','')", (i, f"P{i}", f"P{i}", addr))
        conn.execute("INSERT INTO messages (id, contact_id, step_id, mailbox_id, state,"
                     " to_addr, subject, recipient_count)"
                     " VALUES (?,?,'s','m','drafted',?,'Subject A',1)", (i, i, addr))
    conn.commit()

    # Only the first is in Sent.
    monkeypatch.setattr(reconcile, "find_sent",
                        lambda provider, drafts: ({1}, "Sent", ""))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    result = reconcile.reconcile(conn, cfg, object(), bounce_scan=_no_bounces)

    assert [addr for _i, addr in result.marked] == ["gone@acme.test"]
    states = dict(conn.execute("SELECT to_addr, state FROM messages"))
    assert states["gone@acme.test"] == "sent"
    assert states["waiting@acme.test"] == "drafted"


def test_a_failed_sent_check_does_not_mark_anything(tmp_path, monkeypatch):
    """If the mailbox cannot be read, nothing is assumed either way."""
    from pathlib import Path

    from scripts import reconcile
    from scripts.config import Config
    from scripts.db import open_db

    conn = open_db(str(tmp_path / "r2.db"))
    monkeypatch.setattr(reconcile, "find_sent",
                        lambda provider, drafts: (set(), "", "ConnectionError"))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    result = reconcile.reconcile(conn, cfg, object())
    assert result.marked == [] and result.error == "ConnectionError"


def _bounced(*addrs):
    """A mailbox where Gmail filed a Sent copy and then bounced it back."""
    from scripts.bounces import Bounce, BounceReport

    def scan(provider, since):
        return BounceReport(limit=[Bounce(kind="limit", to_addr=a) for a in addrs])
    return scan


def _two_drafts(tmp_path, monkeypatch):
    from pathlib import Path

    from scripts.db import open_db

    monkeypatch.setenv("OUTBOUND_DB", str(tmp_path / "t.db"))
    conn = open_db(tmp_path / "t.db")
    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at) VALUES (1,'Acme','acme','t','active','','')")
    for i, addr in enumerate(("gone@acme.test", "bounced@acme.test"), start=1):
        conn.execute("INSERT INTO contacts (id, account_id, name, first_name, last_name,"
                     " title, email, email_domain, email_basis, confidence,"
                     " created_at, updated_at) VALUES (?,1,?,?,'X','R',?,'acme.test',"
                     "'observed',0.9,'','')", (i, f"P{i}", f"P{i}", addr))
        conn.execute("INSERT INTO messages (id, contact_id, step_id, mailbox_id, state,"
                     " to_addr, subject, recipient_count)"
                     " VALUES (?,?,'s','m','drafted',?,'Subject A',1)", (i, i, addr))
    conn.commit()
    return conn


def test_a_sent_copy_that_bounced_is_not_a_send(tmp_path, monkeypatch):
    """Over its limit, Gmail writes a Sent copy and *then* refuses to deliver.

    Both messages are in Sent. Only one actually reached anyone. Marking the
    other 'sent' would exclude that contact from every future run for good --
    which is exactly what happened to 96 people on 2026-08-31/09-01.
    """
    from pathlib import Path

    from scripts import reconcile
    from scripts.config import Config

    conn = _two_drafts(tmp_path, monkeypatch)
    monkeypatch.setattr(reconcile, "find_sent",
                        lambda provider, drafts: ({1, 2}, "Sent", ""))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    result = reconcile.reconcile(conn, cfg, object(),
                                 bounce_scan=_bounced("bounced@acme.test"))

    assert [addr for _i, addr in result.marked] == ["gone@acme.test"]
    assert result.withheld == 1
    states = dict(conn.execute("SELECT to_addr, state FROM messages"))
    assert states["gone@acme.test"] == "sent"
    assert states["bounced@acme.test"] == "drafted"


def test_an_unreadable_inbox_marks_nothing(tmp_path, monkeypatch):
    """Unverifiable is not clean. Fail closed rather than re-run the bug."""
    from pathlib import Path

    from scripts import reconcile
    from scripts.bounces import BounceReport
    from scripts.config import Config

    conn = _two_drafts(tmp_path, monkeypatch)
    monkeypatch.setattr(reconcile, "find_sent",
                        lambda provider, drafts: ({1, 2}, "Sent", ""))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    result = reconcile.reconcile(
        conn, cfg, object(),
        bounce_scan=lambda provider, since: BounceReport(error="ConnectionError"))

    assert result.marked == []
    assert "bounce scan failed" in result.error
    states = dict(conn.execute("SELECT to_addr, state FROM messages"))
    assert set(states.values()) == {"drafted"}


def test_clear_deletes_rows_so_the_contact_can_be_drafted_again(tmp_path, monkeypatch):
    """A surviving row of any state blocks the re-draft: idempotency_key is UNIQUE."""
    from scripts import bounces as B

    conn = _two_drafts(tmp_path, monkeypatch)
    conn.execute("UPDATE messages SET state='sent' WHERE to_addr='bounced@acme.test'")
    conn.commit()

    report = B.scan.__wrapped__ if hasattr(B.scan, "__wrapped__") else None
    rep = _bounced("bounced@acme.test")(object(), "01-Aug-2026")
    rows = B.undelivered_rows(conn, rep)
    assert [addr for _i, addr in rows] == ["bounced@acme.test"]

    assert B.clear(conn, rows) == 1
    left = dict(conn.execute("SELECT to_addr, state FROM messages"))
    assert "bounced@acme.test" not in left
    assert left["gone@acme.test"] == "drafted"


def test_only_sent_rows_are_candidates_for_clearing(tmp_path, monkeypatch):
    """A draft that bounced is still a draft; deleting it would lose the copy."""
    from scripts import bounces as B

    conn = _two_drafts(tmp_path, monkeypatch)  # both left 'drafted'
    rep = _bounced("bounced@acme.test")(object(), "01-Aug-2026")
    assert B.undelivered_rows(conn, rep) == []


def test_our_own_cc_addresses_are_not_counted_as_failed_prospects():
    """A DSN names all three recipients; two of them are us."""
    import email as _email

    from scripts import bounces as B

    raw = (
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\n"
        "Subject: Delivery Status Notification (Failure)\r\n"
        "X-Failed-Recipients: dead@acme.test, jais@berkeley.edu\r\n"
        "\r\n"
        "failed\r\n")
    msg = _email.message_from_string(raw)
    assert set(B._failed_recipients(msg)) == {"dead@acme.test", "jais@berkeley.edu"}


def test_two_campaigns_can_be_sendable_at_once(tmp_path, monkeypatch):
    """Testing one campaign must not re-block another.

    The gate used to read the single newest passing test send, so proving
    `startup` immediately un-proved `frontier-lab`. Alternating cost a real
    send each way and made a two-campaign schedule impossible.
    """
    from pathlib import Path

    from scripts import send_queue, templates
    from scripts.config import Config
    from scripts.db import open_db

    conn = open_db(str(tmp_path / "g.db"))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")

    a = templates.template_hash(cfg, "startup")
    b = templates.template_hash(cfg, "frontier-lab")
    for camp, h in (("startup", a), ("frontier-lab", b)):
        conn.execute("INSERT INTO test_sends (mailbox_id, step_id, campaign,"
                     " template_hash, to_addr, ok, sent_at)"
                     " VALUES ('gmail-smtp','step1_initial',?,?,'me@test',1,'')",
                     (camp, h))
    conn.commit()

    # startup was proven first and frontier-lab most recently; both hold.
    for camp in ("startup", "frontier-lab"):
        problems = send_queue.gate(conn, cfg, camp, "gmail-smtp")
        assert not [p for p in problems if "test send" in p], (camp, problems)


def test_an_untested_template_is_still_refused(tmp_path, monkeypatch):
    """Relaxing 'newest' must not relax 'proven at all'."""
    from pathlib import Path

    from scripts import send_queue
    from scripts.config import Config
    from scripts.db import open_db

    conn = open_db(str(tmp_path / "g2.db"))
    cfg = Config(Path(__file__).resolve().parent.parent / "config")
    conn.execute("INSERT INTO test_sends (mailbox_id, step_id, campaign,"
                 " template_hash, to_addr, ok, sent_at)"
                 " VALUES ('gmail-smtp','step1_initial','startup','stale','me@test',1,'')")
    conn.commit()

    problems = send_queue.gate(conn, cfg, "startup", "gmail-smtp")
    assert any("have changed since they last passed a test send" in p for p in problems)
