"""When can a batch go out all at once without tripping the rolling limit.

The hourly drip spreads sends so the trailing 24h never spikes. A burst is the
opposite: everything within seconds, then nothing. Both are safe under the same
rule -- the trailing 24h must stay under the cap -- but a burst has to be *timed*
against what already went out, because it spends its whole budget in one instant.

The arithmetic is a simulation, not a formula. Gmail's window slides: budget
comes back the moment an old send falls off the far edge, so the earliest legal
burst time is exactly when enough of the past has aged out. Anything that does
not fit lands in the next cycle, which is the same question asked again from
the new state.

Nothing here sends. It reads history and prints times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WINDOW = timedelta(hours=24)


@dataclass
class Batch:
    at: datetime
    messages: int
    recipients: int
    used_before: int          # recipients already in the trailing 24h at `at`
    waited_for: datetime | None = None   # the send whose ageing-out unblocked this


@dataclass
class Plan:
    cap: int
    batches: list[Batch] = field(default_factory=list)
    unplaced: int = 0

    @property
    def messages(self) -> int:
        return sum(b.messages for b in self.batches)


def history(conn, mailbox_id: str, since: datetime) -> list[tuple[datetime, int]]:
    """(when, recipients) for everything this mailbox actually put on the wire.

    Test sends are included for the same reason the cap counts them: they are
    real mail out of the same account and Gmail bills them identically.
    """
    iso = since.isoformat(timespec="seconds")
    out = [
        (datetime.fromisoformat(r[0]), r[1])
        for r in conn.execute(
            "SELECT sent_at, recipient_count FROM messages"
            " WHERE mailbox_id=? AND state='sent' AND sent_at > ?",
            (mailbox_id, iso))
        if r[0]
    ]
    out += [
        (datetime.fromisoformat(r[0]), 1)
        for r in conn.execute(
            "SELECT sent_at FROM test_sends WHERE mailbox_id=? AND ok=1 AND sent_at > ?",
            (mailbox_id, iso))
        if r[0]
    ]
    return sorted(out)


def _in_window(events: list[tuple[datetime, int]], at: datetime) -> int:
    return sum(n for when, n in events if at - WINDOW < when <= at)


def _next_open(events: list[tuple[datetime, int]], at: datetime) -> datetime | None:
    """The next instant budget is released: when the oldest in-window send ages out."""
    live = [when for when, _ in events if at - WINDOW < when <= at]
    if not live:
        return None
    # A hair past, so the send is strictly outside the window rather than on its edge.
    return min(live) + WINDOW + timedelta(seconds=1)


def plan(
    conn,
    mailbox_id: str,
    *,
    cap: int,
    pending: list[int],
    now: datetime | None = None,
    window_start=None,
    window_end=None,
    days: set[int] | None = None,
    tz: str = "America/Los_Angeles",
    max_batches: int = 12,
) -> Plan:
    """Earliest burst times for `pending` (a recipient count per message).

    `window_start`/`window_end`/`days` restrict bursts to sociable hours. Quota
    does not care what time it is, but a recipient does, and a 3am burst reads
    as machinery to both the human and the filter.
    """
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    events = history(conn, mailbox_id, now - WINDOW)
    out = Plan(cap=cap)
    queue = list(pending)
    at = now

    def sociable(t: datetime) -> datetime:
        """Move `t` forward to the next moment inside the sending window."""
        if window_start is None:
            return t
        local = t.astimezone(zone)
        for _ in range(14):          # at most a fortnight of skipping
            if days is not None and local.weekday() not in days:
                local = (local + timedelta(days=1)).replace(
                    hour=window_start.hour, minute=window_start.minute, second=0, microsecond=0)
                continue
            if local.time() < window_start:
                local = local.replace(hour=window_start.hour, minute=window_start.minute,
                                      second=0, microsecond=0)
                continue
            if local.time() > window_end:
                local = (local + timedelta(days=1)).replace(
                    hour=window_start.hour, minute=window_start.minute, second=0, microsecond=0)
                continue
            return local.astimezone(timezone.utc)
        return local.astimezone(timezone.utc)

    while queue and len(out.batches) < max_batches:
        # Aim for the largest burst the cap can ever hold, and wait for it.
        #
        # Filling whatever headroom exists right now is the wrong greed: budget
        # is released one aged-out send at a time, so a greedy planner emits a
        # one-message "burst" every two minutes -- which is the hourly drip
        # again, wearing a different name. Waiting until a full cap's worth has
        # aged out is what makes a burst a burst.
        widest = max(queue)
        target = min(len(queue), cap // widest)
        want = sum(queue[:target])

        guard = 0
        while guard < 400:
            guard += 1
            at = sociable(at)
            if cap - _in_window(events, at) >= want:
                break
            nxt = _next_open(events, at)
            if nxt is None or nxt <= at:
                break        # window already empty; cap cannot hold `want` at all
            at = nxt

        used = _in_window(events, at)
        room = cap - used
        took, recips = 0, 0
        for n in queue:
            if recips + n > room:
                break
            recips += n
            took += 1
        if took == 0:
            break            # cap smaller than one message; nothing will ever fit
        blocker = _next_open(events, at) if used else None
        out.batches.append(Batch(at=at, messages=took, recipients=recips,
                                 used_before=used, waited_for=blocker))
        # The burst itself is now history, and it all lands at the same instant.
        events += [(at, n) for n in queue[:took]]
        events.sort()
        del queue[:took]
        nxt = _next_open(events, at)
        at = nxt if nxt and nxt > at else at + WINDOW

    out.unplaced = len(queue)
    return out


def render(p: Plan, tz: str = "America/Los_Angeles") -> str:
    zone = ZoneInfo(tz)
    lines = [f"cap {p.cap} recipients per rolling 24h", ""]
    for i, b in enumerate(p.batches, 1):
        local = b.at.astimezone(zone)
        lines.append(f"cycle {i}: {local:%a %d %b %H:%M %Z}  "
                     f"{b.messages} messages / {b.recipients} recipients"
                     f"  (window holds {b.used_before} before, "
                     f"{b.used_before + b.recipients}/{p.cap} after)")
    lines.append("")
    lines.append(f"{p.messages} messages placed across {len(p.batches)} cycles")
    if p.unplaced:
        lines.append(f"{p.unplaced} did not fit inside the horizon")
    return "\n".join(lines)
