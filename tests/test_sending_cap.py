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
