"""A burst has to be timed against history, not just sized against the cap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.burst_plan import plan


def _sent(conn, when: datetime, n: int, recipients: int = 3) -> None:
    conn.execute("INSERT INTO accounts (id, name, name_normalized, source, status,"
                 " created_at, updated_at) VALUES (1,'Acme','acme','t','active','','')"
                 " ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO contacts (id, account_id, name, first_name, last_name, title,"
                 " email, email_domain, email_basis, confidence, created_at, updated_at)"
                 " VALUES (1,1,'A B','A','B','R','a@b.test','b.test','observed',0.9,'','')"
                 " ON CONFLICT DO NOTHING")
    for _ in range(n):
        conn.execute("INSERT INTO messages (contact_id, step_id, mailbox_id, state, to_addr,"
                     " recipient_count, sent_at) VALUES (1,'step1_initial','gmail-smtp','sent',"
                     "'a@b.test',?,?)", (recipients, when.isoformat(timespec="seconds")))
    conn.commit()


def test_an_empty_history_bursts_immediately(conn):
    now = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    p = plan(conn, "gmail-smtp", cap=400, pending=[3] * 50, now=now)
    assert len(p.batches) == 1
    assert p.batches[0].at == now
    assert p.batches[0].messages == 50


def test_a_full_window_waits_for_the_oldest_send_to_age_out(conn):
    """The cap is a sliding window, so the earliest legal burst is exactly when
    enough of the past has fallen off the far edge -- not 'tomorrow'."""
    now = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    _sent(conn, now - timedelta(hours=23), 133)      # 399 recipients, window full
    p = plan(conn, "gmail-smtp", cap=400, pending=[3] * 133, now=now)
    assert len(p.batches) == 1
    # Aged out one hour and one second after `now`, not a flat 24h from now.
    assert p.batches[0].at == now + timedelta(hours=1, seconds=1)
    assert p.batches[0].used_before == 0


def test_the_overflow_lands_in_the_next_cycle_a_day_later(conn):
    now = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    p = plan(conn, "gmail-smtp", cap=400, pending=[3] * 300, now=now)
    assert [b.messages for b in p.batches] == [133, 133, 34]
    assert p.unplaced == 0
    gaps = [(b.at - a.at) for a, b in zip(p.batches, p.batches[1:])]
    assert all(g >= timedelta(hours=24) for g in gaps), "cycles must not overlap the window"


def test_a_burst_never_exceeds_the_cap(conn):
    now = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    _sent(conn, now - timedelta(hours=2), 40)        # 120 recipients still live
    p = plan(conn, "gmail-smtp", cap=400, pending=[3] * 200, now=now)
    for b in p.batches:
        assert b.used_before + b.recipients <= 400


def test_the_planner_does_not_nibble(conn):
    """Budget comes back one aged-out send at a time. A planner that spends each
    sliver as it appears emits one-message 'bursts' minutes apart, which is the
    hourly drip again under another name."""
    now = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    for i in range(133):                              # staggered, window full
        _sent(conn, now - timedelta(hours=23) + timedelta(minutes=i), 1)
    p = plan(conn, "gmail-smtp", cap=400, pending=[3] * 133, now=now)
    assert len(p.batches) == 1, [b.messages for b in p.batches]
    assert p.batches[0].messages == 133


def test_the_sending_window_moves_a_burst_off_a_weekend(conn):
    """Quota does not care what time it is; a recipient does. A 3am Sunday burst
    reads as machinery to both the human and the filter."""
    from datetime import time

    # Saturday 07:00 UTC.
    now = datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
    p = plan(conn, "gmail-smtp", cap=400, pending=[3] * 10, now=now,
             window_start=time(7, 0), window_end=time(21, 0), days={0, 1, 2, 3, 4})
    local = p.batches[0].at.astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    assert local.weekday() == 0, f"expected Monday, got {local:%A %H:%M}"
    assert local.time() >= time(7, 0)
