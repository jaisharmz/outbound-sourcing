"""Who is already working on a company, or has already written to a person.

Five people sharing a target list will collide, and two emails from one club to
one person in a week reads as disorganised and wastes the contact. "Split the
list in Slack" survives about a fortnight.

What this is: one append-only CSV, in a location the club already shares -- a
git repo, a Drive folder, Dropbox. No server, no database, no sync logic. Pull
before a run, push after.

Append-only matters. Two people claiming different companies produce two new
lines at the end of the file, which git merges without a conflict and Drive
resolves by keeping both. Rewriting or sorting the file would turn every
concurrent edit into a conflict, so nothing here ever rewrites it.

Two kinds of collision, because they happen differently:

  company   two members researching the same company at once, wasting both
            their time before either sends anything
  person    two members reaching the same person from different companies --
            a researcher with two affiliations, or someone who moved -- which
            no company-level check would catch

Claims go stale. Someone who claimed a company and never worked it should not
hold it forever, so a claim older than `stale_after_days` is reported as stale
rather than blocking. It is still shown, because "Ada looked at this two months
ago" is useful even when it no longer reserves anything.

Sending records a claim automatically. An explicit `outbound claim` exists for
"I am working on this now", but the common case needs no new habit -- and a
mechanism that depends on remembering is the one that fails.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

COMPANY, PERSON = "company", "person"
HEADER = ["kind", "value", "who", "claimed_at", "note"]


@dataclass
class Claim:
    kind: str
    value: str
    who: str
    claimed_at: str
    note: str = ""

    def age_days(self, now: datetime | None = None) -> int:
        try:
            when = datetime.fromisoformat(self.claimed_at.replace("Z", "+00:00"))
        except ValueError:
            return 10_000
        return ((now or datetime.now(timezone.utc)) - when).days

    def is_stale(self, stale_after_days: int, now: datetime | None = None) -> bool:
        return self.age_days(now) > stale_after_days

    def describe(self, stale_after_days: int) -> str:
        age = self.age_days()
        when = "today" if age == 0 else f"{age}d ago"
        tail = " (stale)" if self.is_stale(stale_after_days) else ""
        return f"{self.who}, {when}{tail}"


def _key(kind: str, value: str) -> str:
    return f"{kind}:{value.strip().lower()}"


def whoami() -> str:
    """Whoever is running this. Not a login -- just a label in a shared file."""
    return (os.environ.get("OUTBOUND_USER")
            or os.environ.get("USER")
            or os.environ.get("USERNAME")
            or "unknown")


def load(path: Path | str | None) -> list[Claim]:
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.exists():
        return []
    out = []
    with p.open(newline="") as fh:
        for row in csv.DictReader(fh):
            if not row.get("value"):
                continue
            out.append(Claim(row.get("kind") or COMPANY, row["value"],
                             row.get("who") or "unknown",
                             row.get("claimed_at") or "", row.get("note") or ""))
    return out


def add(path: Path | str, kind: str, value: str, who: str | None = None,
        note: str = "") -> Claim:
    """Append one claim. Never rewrites, so concurrent edits merge cleanly."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    fresh = not p.exists() or p.stat().st_size == 0
    claim = Claim(kind, value.strip(), who or whoami(),
                  datetime.now(timezone.utc).isoformat(timespec="seconds"), note)
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if fresh:
            w.writerow(HEADER)
        w.writerow([claim.kind, claim.value, claim.who, claim.claimed_at, claim.note])
    return claim


def held_by_others(path: Path | str | None, kind: str, value: str,
                   me: str | None = None) -> list[Claim]:
    """Claims on this thing by anyone else, newest first."""
    me = (me or whoami()).lower()
    want = _key(kind, value)
    mine_excluded = [c for c in load(path)
                     if _key(c.kind, c.value) == want and c.who.lower() != me]
    return sorted(mine_excluded, key=lambda c: c.claimed_at, reverse=True)


def warn_line(claims: list[Claim], kind: str, value: str,
              stale_after_days: int) -> str:
    """One line for the operator, or empty when nothing is held."""
    if not claims:
        return ""
    live = [c for c in claims if not c.is_stale(stale_after_days)]
    shown = live or claims
    who = "; ".join(c.describe(stale_after_days) for c in shown[:3])
    if not live:
        return (f"{value} was claimed by {who}. All of those are stale, so it is "
                f"probably free.")
    return f"{value} is already claimed by {who}."
