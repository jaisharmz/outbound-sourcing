"""Find messages Gmail accepted, filed in Sent, and then refused to deliver.

`reconcile` treats a copy in the Sent folder as proof a draft went out. That is
wrong in one specific and costly way. When the account is over its sending
limit, Gmail does not reject at SMTP time: it accepts the message, writes the
Sent copy, and *then* returns a bounce to the inbox reading "You have reached a
limit for sending mail. Your message was not sent." The Sent copy stays. So the
recipient never heard from us, reconcile marks them contacted anyway, and
`due()` excludes them from every future run -- the exact false positive
reconcile's own docstring warns about, arriving through a door it did not check.

Two kinds of bounce come back, and they call for opposite responses:

  limit      -- nothing was delivered and nothing is wrong with the address.
                The message row must be cleared so the contact can be drafted
                again.
  permanent  -- the address does not exist or the domain refused us. The send
                really happened and consumed quota; re-drafting would waste
                another one. These belong in suppression, not back in the queue.

Limit bounces carry no `message/rfc822` part and no `X-Failed-Recipients`, so
the recipient cannot be read off the bounce itself. They are threaded replies
though: `In-Reply-To` holds the Message-ID of the Sent copy, and that copy has
the `To`. Joining through the Sent folder is what makes them attributable.
"""

from __future__ import annotations

import email
import email.utils
from dataclasses import dataclass, field

LIMIT_PHRASE = "you have reached a limit for sending mail"

SENT_FOLDERS = ('"[Gmail]/Sent Mail"', '"[Gmail]/Sent"', "Sent", "INBOX.Sent")

# Gmail's own daemon and the remote MTAs that bounce back to us.
DAEMON = "mailer-daemon"


@dataclass
class Bounce:
    kind: str                 # "limit" | "permanent"
    to_addr: str
    subject: str = ""
    sent_date: str = ""
    bounce_date: str = ""


@dataclass
class BounceReport:
    limit: list[Bounce] = field(default_factory=list)
    permanent: list[Bounce] = field(default_factory=list)
    scanned: int = 0
    unjoined: int = 0
    error: str = ""


def _text(msg) -> str:
    out = []
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                out.append(part.get_payload(decode=True).decode("utf8", "replace"))
            except Exception:
                pass
    return "\n".join(out)


def _addrs(value: str) -> list[str]:
    return [a[1].lower() for a in email.utils.getaddresses([value or ""]) if a[1]]


def _index_sent(imap) -> tuple[dict[str, dict], str]:
    """Message-ID -> {to, subject, date} for everything in the Sent folder.

    The join key for limit bounces. Without it they are anonymous.
    """
    index: dict[str, dict] = {}
    folder_used = ""
    for folder in SENT_FOLDERS:
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            continue
        folder_used = folder
        typ, data = imap.search(None, "ALL")
        uids = data[0].split() if data and data[0] else []
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i:i + 200])
            typ, resp = imap.fetch(
                chunk, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID TO SUBJECT DATE)])")
            for item in resp or []:
                if not isinstance(item, tuple):
                    continue
                msg = email.message_from_bytes(item[1])
                mid = (msg.get("Message-ID") or "").strip()
                to = _addrs(msg.get("To", ""))
                if mid and to:
                    index[mid] = {"to": to[0], "subject": msg.get("Subject") or "",
                                  "date": msg.get("Date") or ""}
        break
    return index, folder_used


def _failed_recipients(msg) -> list[str]:
    """Who a delivery-status bounce says failed. Empty for limit bounces."""
    failed: list[str] = []
    xf = msg.get("X-Failed-Recipients")
    if xf:
        failed += _addrs(xf)
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for sub in part.get_payload():
                raw = sub.get("Final-Recipient") or sub.get("Original-Recipient")
                if raw:
                    addr = raw.split(";")[-1].strip().lower()
                    if addr:
                        failed.append(addr)
    return failed


def scan(provider, *, since: str, ours: set[str] | None = None) -> BounceReport:
    """Every bounce since `since` (IMAP date, e.g. "20-Aug-2026"), classified.

    `ours` is the operator's own addresses -- From, Reply-To and every standing
    CC. A delivery-status bounce names all three recipients of the message that
    failed, so without this the CC'd Berkeley and club addresses get counted as
    failed prospects and the permanent total reads triple.
    """
    report = BounceReport()
    ours = {a.lower() for a in (ours or set())}
    try:
        imap = provider._imap()
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    try:
        sent_index, _ = _index_sent(imap)

        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            report.error = "could not select INBOX"
            return report
        typ, data = imap.search(None, "SINCE", since, "FROM", DAEMON)
        uids = data[0].split() if data and data[0] else []
        report.scanned = len(uids)

        for i in range(0, len(uids), 50):
            chunk = b",".join(uids[i:i + 50])
            typ, resp = imap.fetch(chunk, "(RFC822)")
            for item in resp or []:
                if not isinstance(item, tuple):
                    continue
                msg = email.message_from_bytes(item[1])
                bounce_date = msg.get("Date") or ""
                if LIMIT_PHRASE in _text(msg).lower():
                    # Attributable only through the thread it replies to.
                    refs = (msg.get("In-Reply-To") or msg.get("References") or "").split()
                    hit = next((sent_index[r] for r in refs if r in sent_index), None)
                    if hit is None:
                        report.unjoined += 1
                        continue
                    report.limit.append(Bounce(
                        kind="limit", to_addr=hit["to"], subject=hit["subject"],
                        sent_date=hit["date"], bounce_date=bounce_date))
                else:
                    for addr in _failed_recipients(msg):
                        if addr in ours:
                            continue
                        report.permanent.append(Bounce(
                            kind="permanent", to_addr=addr, bounce_date=bounce_date))
    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return report


def undelivered_rows(conn, report: BounceReport) -> list[tuple[int, str]]:
    """Message rows marked sent whose send actually limit-bounced.

    Matched on recipient alone rather than recipient+subject: the bounce carries
    the subject of the Sent copy, and that is the same string the row holds, but
    a row is only a candidate if it is `sent` in the first place, and a contact
    has at most one step1 message. Recipient is enough to be unambiguous and
    survives a subject the operator edited by hand before sending.
    """
    out: list[tuple[int, str]] = []
    for b in report.limit:
        for row in conn.execute(
                "SELECT id, to_addr FROM messages"
                " WHERE lower(to_addr) = ? AND state = 'sent'", (b.to_addr,)):
            out.append((row[0], row[1]))
    return sorted(set(out))


def clear(conn, rows: list[tuple[int, str]]) -> int:
    """Delete falsely-sent rows so `send` can draft the contact again.

    Deleting rather than flipping state back: `idempotency_key` is UNIQUE, and
    `send_one` inserts a fresh row per attempt, so a surviving row of any state
    blocks the re-draft. This is the same remedy the `draft.vanished` events
    used for drafts Gmail discarded outright.

    `mailbox_day` is deliberately left alone. Those attempts did reach Gmail and
    did consume the account's daily allowance, whatever happened afterwards --
    zeroing the ledger would invite the throttle to overshoot all over again.
    """
    from .db import log_event, utcnow

    if not rows:
        return 0
    ids = [r[0] for r in rows]
    marks = ",".join("?" * len(ids))
    contacts = [r[0] for r in conn.execute(
        f"SELECT contact_id FROM messages WHERE id IN ({marks})", ids)]
    conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
    for cid in contacts:
        conn.execute("UPDATE contacts SET status='new', updated_at=? WHERE id=?",
                     (utcnow(), cid))
    log_event(conn, "info", "bounce.limit_cleared", count=len(ids),
              reason="Gmail filed a Sent copy then bounced with its sending limit;"
                     " the recipient never received it",
              message_ids=",".join(str(i) for i in ids))
    conn.commit()
    return len(ids)
