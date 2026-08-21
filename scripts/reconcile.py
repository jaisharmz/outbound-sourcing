"""Notice what the operator has already sent, without being told.

Drafting is not sending, and the two must stay distinct: a batch prepared and
abandoned must not suppress companies nobody wrote to. But making the operator
announce each send is a manual step that fails silently when skipped -- the
contact stays "new", a later run re-queues them, and they get a second email.

So the tool looks instead of asking. Every draft it wrote is in Gmail; when one
is sent it appears in the Sent folder. Matching is on recipient plus subject
rather than Message-ID, because Gmail assigns a fresh Message-ID when a draft is
sent from the web client -- the id we generated at APPEND time is not the id that
goes out, which is exactly why this was a manual step to begin with.

That match is deliberately narrow. A false positive marks someone contacted who
was not, which suppresses them and stops the follow-up; a false negative just
means the operator's next run reconciles it. So both the address and the exact
subject must match, and only messages in Sent count.
"""

from __future__ import annotations

import email
import email.policy
import sqlite3
from dataclasses import dataclass

from .db import log_event, utcnow

SENT_FOLDERS = ('"[Gmail]/Sent Mail"', '"[Gmail]/Sent"', "Sent", "INBOX.Sent")


@dataclass
class Reconciled:
    marked: list[tuple[int, str]]        # (message_id, to_addr)
    scanned: int = 0
    folder: str = ""
    error: str = ""


def _drafted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, contact_id, to_addr, subject, mailbox_id, recipient_count"
        "  FROM messages WHERE state = 'drafted'").fetchall()


def find_sent(provider, drafts: list[sqlite3.Row]) -> tuple[set[int], str, str]:
    """Which drafted messages now appear in the Sent folder."""
    if not drafts:
        return set(), "", ""
    try:
        imap = provider._imap()
    except Exception as exc:
        return set(), "", f"{type(exc).__name__}: {exc}"

    matched: set[int] = set()
    folder_used = ""
    try:
        for folder in SENT_FOLDERS:
            status, _ = imap.select(folder, readonly=True)
            if status != "OK":
                continue
            folder_used = folder
            for row in drafts:
                if not row["subject"]:
                    continue
                # Both must match. A subject alone would catch the whole batch;
                # a recipient alone would catch any mail ever sent to them.
                typ, data = imap.search(
                    None, "TO", f'"{row["to_addr"]}"', "SUBJECT", f'"{row["subject"]}"')
                if typ == "OK" and data and data[0].split():
                    matched.add(row["id"])
            break
    except Exception as exc:
        return matched, folder_used, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return matched, folder_used, ""


def reconcile(conn: sqlite3.Connection, config, provider) -> Reconciled:
    """Mark as sent every draft that has since left. Safe to run repeatedly."""
    drafts = _drafted(conn)
    matched, folder, error = find_sent(provider, drafts)
    out = Reconciled(marked=[], scanned=len(drafts), folder=folder, error=error)
    if not matched:
        return out

    from . import claims as C

    for row in drafts:
        if row["id"] not in matched:
            continue
        conn.execute("UPDATE messages SET state='sent', sent_at=? WHERE id=?",
                     (utcnow(), row["id"]))
        conn.execute("UPDATE contacts SET status='active', updated_at=? WHERE id=?",
                     (utcnow(), row["contact_id"]))
        conn.execute(
            "INSERT INTO mailbox_day (mailbox_id, day, messages, recipients)"
            " VALUES (?,?,1,?) ON CONFLICT(mailbox_id, day) DO UPDATE SET"
            " messages=messages+1, recipients=recipients+excluded.recipients",
            (row["mailbox_id"], utcnow()[:10], row["recipient_count"]))
        if config.campaign.claims_file:
            C.add(config.campaign.claims_file, C.PERSON, row["to_addr"], note="sent")
        out.marked.append((row["id"], row["to_addr"]))

    log_event(conn, "info", "reconcile.sent", found=len(out.marked), folder=folder)
    return out
