"""The cap has to follow Gmail's clock, not a calendar.

Gmail meters a rolling 24h. Counting per calendar day agrees with that only
while every send happens in one contiguous block at the same hour each day --
which is exactly the assumption that broke when the sending window widened past
17:00 PT, where the UTC date rolls over. These pin the difference.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts import send_queue


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _sent(conn, when: datetime, recipients: int = 3, mailbox: str = "gmail-smtp") -> None:
    conn.execute("INSERT INTO messages (contact_id, step_id, mailbox_id, state, to_addr,"
                 " recipient_count, sent_at) VALUES (1,'step1_initial',?,'sent','a@b.test',?,?)",
                 (mailbox, recipients, _iso(when)))
    conn.commit()


def _one_contact(conn) -> None:
    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at) VALUES (1,'Acme','acme','t','active','','')")
    conn.execute("INSERT INTO contacts (id, account_id, name, first_name, last_name, title,"
                 " email, email_domain, email_basis, confidence, created_at, updated_at)"
                 " VALUES (1,1,'A B','A','B','Researcher','a@b.test','b.test','observed',"
                 " 0.9,'','')")
    conn.commit()


def test_sends_older_than_the_window_stop_counting(conn):
    """The budget is a moving window, so it has to free up as sends age out --
    otherwise the queue stalls permanently the first time it fills."""
    _one_contact(conn)
    now = datetime.now(timezone.utc)
    _sent(conn, now - timedelta(hours=25))
    assert send_queue.sent_in_window(conn, "gmail-smtp") == 0
    _sent(conn, now - timedelta(hours=23))
    assert send_queue.sent_in_window(conn, "gmail-smtp") == 3


def test_crossing_the_utc_date_does_not_refill_the_budget(conn):
    """The bug this replaced. 16:30 and 18:00 Pacific are the same evening but
    different UTC dates, so a per-UTC-day count reset the allowance mid-evening
    and would have released a second full day's sends a few hours after the
    first -- ~800 recipients inside 13 hours, against a ceiling measured at
    ~600."""
    _one_contact(conn)
    # 2026-09-01 16:30 PT and 18:00 PT == 23:30Z on the 1st and 01:00Z on the 2nd.
    before = datetime.now(timezone.utc).replace(hour=23, minute=30, second=0, microsecond=0)
    _sent(conn, before)
    _sent(conn, before + timedelta(hours=1, minutes=30))
    assert before.strftime("%Y-%m-%d") != (before + timedelta(hours=1, minutes=30)).strftime("%Y-%m-%d")
    assert send_queue.sent_in_window(conn, "gmail-smtp") == 6


def test_test_sends_count_against_the_cap(conn):
    """A test send is real mail out of the same mailbox. Leaving it uncounted
    understates the trailing 24h by exactly the sends made while proving the
    templates -- the ones made right before a big batch."""
    _one_contact(conn)
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash,"
                 " to_addr, ok, sent_at) VALUES ('gmail-smtp','step1_initial','startup','h',"
                 "'me@test',1,?)", (_iso(now - timedelta(hours=1)),))
    conn.execute("INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash,"
                 " to_addr, ok, sent_at) VALUES ('gmail-smtp','step1_initial','startup','h',"
                 "'me@test',0,?)", (_iso(now - timedelta(hours=1)),))
    conn.commit()
    # The failed one never reached anybody, so it must not spend budget.
    assert send_queue.sent_in_window(conn, "gmail-smtp") == 1


def test_other_mailboxes_do_not_spend_this_mailbox_budget(conn):
    _one_contact(conn)
    _sent(conn, datetime.now(timezone.utc) - timedelta(hours=1), mailbox="other")
    assert send_queue.sent_in_window(conn, "gmail-smtp") == 0


def test_drafts_do_not_spend_budget(conn):
    """Drafting is the whole queue; if it counted, preparing a batch would
    consume the allowance for sending it."""
    _one_contact(conn)
    conn.execute("INSERT INTO messages (contact_id, step_id, mailbox_id, state, to_addr,"
                 " recipient_count, drafted_at) VALUES (1,'step1_initial','gmail-smtp',"
                 "'drafted','a@b.test',3,?)",
                 (_iso(datetime.now(timezone.utc)),))
    conn.commit()
    assert send_queue.sent_in_window(conn, "gmail-smtp") == 0


def test_a_recipient_budget_is_not_a_message_budget():
    """`due` counts contacts, the cap counts recipients. Dividing by the widest
    a message can get is what stops a batch of 17 walking into 30 recipients of
    headroom and putting 51 on the wire."""
    from scripts.config import CCConfig, CCRule

    cc = CCConfig(default=CCRule(cc=["a@x.test", "b@x.test"]))
    assert cc.max_recipients_per_message() == 3

    # A per-campaign rule wider than the default sets the worst case.
    cc = CCConfig(default=CCRule(cc=["a@x.test"]),
                  by_campaign={"startup": CCRule(cc=["a@x.test", "b@x.test", "c@x.test"])})
    assert cc.max_recipients_per_message() == 4

    assert CCConfig().max_recipients_per_message() == 1


def test_a_draft_is_deleted_through_the_mailbox_that_holds_it(monkeypatch, tmp_path):
    """Two senders, one drafts folder.

    Nearly the whole queue was drafted from gmail-smtp. When a second mailbox
    starts sending those contacts, the delete has to go back to gmail-smtp. Sent
    through the new sender it searches the wrong folder, finds nothing, and
    leaves the draft sitting where the operator clicks -- a second copy to
    someone who has already been written to, which is the exact failure the
    delete exists to prevent."""
    from scripts import send_queue
    from tests.test_drafts import _Recorder, _fixture

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    drafter = _Recorder()
    sender = _Recorder()

    # Drafted by the mailbox that owns the folder.
    send_queue.send_one(conn, config, drafter, mailbox, row, "startup", 1,
                        dry_run=False, mode="draft")

    # A different mailbox sends it. Give it a distinct id and make the config
    # hand back the drafting mailbox's provider for that id.
    other = mailbox.model_copy(update={"id": "berkeley-smtp"})
    send_queue._DRAFT_PROVIDERS.clear()
    monkeypatch.setattr(send_queue.providers, "build", lambda mb, sec: drafter)

    outcome, detail = send_queue.send_one(conn, config, sender, other, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "done", detail
    assert sender.calls == ["send"], "the new mailbox sends"
    assert drafter.deleted == ["<draft-1@test>"], "the old mailbox deletes its own draft"
    assert sender.deleted == [], "the sender must not be asked for a draft it never held"
    send_queue._DRAFT_PROVIDERS.clear()


def test_an_unknown_draft_mailbox_falls_back_to_the_sender(monkeypatch, tmp_path):
    """A mailbox dropped from config must not raise after the message is gone.
    Degrading to the old behaviour costs a stranded draft, which is reported;
    raising here loses the send record for mail that already left."""
    from scripts import send_queue
    from tests.test_drafts import _Recorder, _fixture

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)
    provider = _Recorder()
    send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                        dry_run=False, mode="draft")
    conn.execute("UPDATE messages SET mailbox_id='deleted-mailbox'")
    conn.commit()

    send_queue._DRAFT_PROVIDERS.clear()
    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row,
                                          "startup", 1, dry_run=False, mode="send")
    assert outcome == "done", detail
    assert provider.deleted == ["<draft-1@test>"]
    send_queue._DRAFT_PROVIDERS.clear()


def test_a_send_that_never_reached_the_server_is_retried(monkeypatch, tmp_path):
    """DNS died mid-slice on 2026-09-02 and five contacts landed in `failed`,
    where nothing ever retries them. But a name that never resolved means no
    SMTP conversation began, so the message provably did not go out -- leaving
    it stranded costs a real person for a wifi blip."""
    from scripts import send_queue
    from scripts.providers import SendResult
    from tests.test_drafts import _Recorder, _fixture

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)

    class Flaky(_Recorder):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def send(self, email):
            self.attempts += 1
            if self.attempts == 1:
                return SendResult(ok=False, error=(
                    "gaierror: [Errno 8] nodename nor servname provided, or not known"))
            return SendResult(ok=True, message_id="<sent-1@test>")

    provider = Flaky()
    outcome, _ = send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                                     dry_run=False, mode="send")
    assert outcome == "failed"
    assert conn.execute("SELECT state FROM messages").fetchone()["state"] == "failed"

    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                                          dry_run=False, mode="send")
    assert outcome == "done", detail
    msg = conn.execute("SELECT state, error, failed_at FROM messages").fetchone()
    assert msg["state"] == "sent"
    assert msg["error"] is None and msg["failed_at"] is None, "stale failure must be cleared"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_an_ambiguous_failure_is_never_retried(monkeypatch, tmp_path):
    """A broken pipe happens mid-conversation, after Gmail may already have
    accepted the message. Retrying one is how a stranger gets it twice."""
    from scripts import send_queue
    from scripts.providers import SendResult
    from tests.test_drafts import _Recorder, _fixture

    row, conn, config, mailbox = _fixture(tmp_path, monkeypatch)

    class Broken(_Recorder):
        def send(self, email):
            self.calls.append("send")
            return SendResult(ok=False, error="abort: socket error: [Errno 32] Broken pipe")

    provider = Broken()
    assert send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                               dry_run=False, mode="send")[0] == "failed"
    outcome, detail = send_queue.send_one(conn, config, provider, mailbox, row, "startup", 1,
                                          dry_run=False, mode="send")
    assert outcome == "held", detail
    assert provider.calls == ["send"], "the provider must not be called a second time"


def test_never_left_is_narrow():
    from scripts.send_queue import never_left

    assert never_left("gaierror: [Errno 8] nodename nor servname provided, or not known")
    assert never_left("ConnectionRefusedError: [Errno 61] Connection refused")
    assert not never_left("TimeoutError: [Errno 60] Operation timed out")
    assert not never_left("abort: socket error: [Errno 32] Broken pipe")
    assert not never_left("SMTPDataError: (550, b'5.4.5 Daily user sending limit exceeded')")
    assert not never_left(None) and not never_left("")
