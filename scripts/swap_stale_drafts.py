"""Delete drafts whose copy predates a template change, once a replacement exists.

IMAP cannot edit a message in place, so changing an existing draft means
appending a corrected one and deleting the stale one. Both carry the same To and
the same Subject -- a copy edit usually does not touch the subject line -- so
recipient+subject, which is how reconcile and everything else here matches,
cannot tell them apart. Only the body can.

The safety property this enforces: **a stale draft is deleted only after a
corrected draft to the same recipient has been seen in the same folder.** There
is never a moment where a recipient has neither, even if the run dies halfway.

A body carrying neither marker is not understood and is never touched -- the
operator's own unrelated drafts live in this folder too.
"""

from __future__ import annotations

import email
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SwapReport:
    total: int = 0
    corrected: int = 0
    stale: int = 0
    unrecognised: int = 0
    deletable: list[tuple[str, bytes]] = field(default_factory=list)
    orphaned: list[tuple[str, bytes]] = field(default_factory=list)
    deleted: int = 0


def _html_body(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode("utf8", "replace")
    return ""


def swap(provider, *, new_marker: str, old_marker: str,
         folder: str = '"[Gmail]/Drafts"', apply: bool = False) -> SwapReport:
    """Find stale drafts superseded by a corrected one, and optionally delete them."""
    report = SwapReport()
    conn = provider._imap()
    try:
        conn.select(folder, readonly=not apply)
        typ, data = conn.search(None, "ALL")
        uids = data[0].split() if data and data[0] else []
        report.total = len(uids)

        stale: dict[str, list[bytes]] = {}
        fresh: Counter = Counter()
        for uid in uids:
            typ, md = conn.fetch(uid, "(RFC822)")
            if not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            to = (msg.get("To") or "").strip().lower()
            body = _html_body(msg)
            if new_marker in body:
                fresh[to] += 1
            elif old_marker in body:
                stale.setdefault(to, []).append(uid)
            else:
                report.unrecognised += 1

        report.corrected = sum(fresh.values())
        report.stale = sum(len(v) for v in stale.values())
        for to, us in stale.items():
            target = report.deletable if fresh.get(to) else report.orphaned
            target.extend((to, u) for u in us)

        if apply and report.deletable:
            for _to, uid in report.deletable:
                conn.store(uid, "+FLAGS", "\\Deleted")
            conn.expunge()
            report.deleted = len(report.deletable)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return report
