"""The send path. One mailbox, invoked by hand, zero model calls.

    python -m scripts.send_queue --dry-run
    python -m scripts.send_queue --limit 20

Crash safety is the point of the state machine: a row commits `sending` before
the provider call and `sent` after, so a crash between the two leaves evidence
rather than a duplicate. Nothing here retries an ambiguous failure.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

from .cc import resolve as resolve_cc
from .config import Config, load_config
from .db import get_or_create_campaign, log_event, open_db, transaction, utcnow
from .errors import ConfigError
from .normalize import display_company
from . import providers, suppression, templates

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def window_open(config: Config, now: datetime | None = None) -> tuple[bool, str]:
    now = now or datetime.now()
    w = config.campaign.sending_window
    if now.weekday() not in {DAYS[d] for d in w.days}:
        return False, f"{now:%A} is not in the sending window ({', '.join(w.days)})"
    if config.blackout.covers(now.date()):
        return False, f"{now:%Y-%m-%d} is a blackout date"
    if not (w.start <= now.time() <= w.end):
        return False, f"{now:%H:%M} is outside {w.start:%H:%M}-{w.end:%H:%M}"
    return True, "open"


CAP_WINDOW_HOURS = 24


def sent_in_window(conn, mailbox_id: str, hours: int = CAP_WINDOW_HOURS) -> int:
    """Recipients this mailbox actually put on the wire in the trailing `hours`.

    Gmail's limit is a rolling 24h, not a calendar day, and the two only agree
    while sending happens in one contiguous block at the same hour each day.
    The old count summed `mailbox_day` for the current *UTC* date, which rolls
    over at 17:00 PT -- mid-afternoon here. That was safe only by accident of
    the 08:00-16:00 window sitting inside one UTC date. Widen the window past
    17:00 and the budget resets mid-evening, handing out a second full day's
    allowance a few hours after the first: ~800 recipients inside 13 hours,
    against a ceiling measured at ~600 on 2026-08-31.

    Counted from `messages` rather than `mailbox_day` because a rolling window
    needs per-send timestamps, not daily totals. Test sends count too: they are
    real mail and Gmail bills them like any other.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    sent = conn.execute(
        "SELECT COALESCE(SUM(recipient_count),0) FROM messages"
        " WHERE mailbox_id=? AND state='sent' AND sent_at > ?",
        (mailbox_id, since)).fetchone()[0]
    tests = conn.execute(
        "SELECT COUNT(*) FROM test_sends WHERE mailbox_id=? AND ok=1 AND sent_at > ?",
        (mailbox_id, since)).fetchone()[0]
    return sent + tests


def gate(conn, config: Config, campaign: str, mailbox_id: str) -> list[str]:
    """Everything that must be true before a single email leaves."""
    problems = list(config.preflight("campaign", campaign=campaign))

    from .check_links import gate as link_gate
    problems.extend(link_gate(conn, config, campaign))

    # The From on the delivered copy, not the one we set. Gmail rewrites From to
    # the authenticated account unless the address is a verified "Send mail as"
    # alias, and the rewrite is invisible from the sending side. Sending 60
    # messages that claim a university address and arrive from a personal gmail
    # is worse than not sending: it is the exact mismatch a recipient reads as a
    # spoof, and it cannot be taken back.
    from email.utils import parseaddr

    mb = config.mailboxes.get(mailbox_id)
    want_from = parseaddr(mb.from_.header())[1].lower()
    row = conn.execute(
        "SELECT headers FROM test_sends WHERE mailbox_id=? AND ok=1 AND headers IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (mailbox_id,)).fetchone()
    if row and row["headers"] and "--- delivered ---" in row["headers"]:
        delivered = row["headers"].split("--- delivered ---", 1)[1]
        got = ""
        for line in delivered.splitlines():
            if line.strip().lower().startswith("from:"):
                got = parseaddr(line.partition(":")[2])[1].lower()
                break
        if got and got != want_from:
            problems.append(
                f"the last test send was delivered as {got}, not the configured "
                f"{want_from}. Gmail rewrites From unless the address is a verified "
                f"'Send mail as' alias, so every recipient would see {got}. "
                f"Verify {want_from} in Gmail (Settings -> Accounts and Import -> "
                f"'Send mail as'), then re-run: outbound test-email --mailbox "
                f"{mailbox_id}")

    # What must be true is that *this copy* was proven deliverable, not that it
    # was the most recent thing tested. Keying on the newest test send alone
    # made campaigns mutually exclusive: testing `startup` re-blocked
    # `frontier-lab` and vice versa, so a mailbox could never have two
    # sendable campaigns at once and every alternation cost a real send.
    h = templates.template_hash(config, campaign)
    ever = conn.execute("SELECT 1 FROM test_sends WHERE mailbox_id=? AND ok=1"
                        " LIMIT 1", (mailbox_id,)).fetchone()
    match = conn.execute(
        "SELECT template_hash, sent_at FROM test_sends WHERE mailbox_id=? AND ok=1"
        " AND template_hash=? ORDER BY id DESC LIMIT 1", (mailbox_id, h)).fetchone()
    if not ever:
        problems.append(f"mailbox {mailbox_id!r} has never passed a test send. "
                        f"Run: outbound test-email --mailbox {mailbox_id}")
    elif not match:
        problems.append(
            f"the {campaign!r} templates have changed since they last passed a test "
            f"send on {mailbox_id!r} (nothing proven for hash {h}). Re-run: "
            f"outbound test-email --mailbox {mailbox_id} --campaign {campaign}")
    return problems


def due(conn, campaign_id: int, limit: int, campaign: str | None = None) -> list:
    """Contacts ready to send FOR THIS CAMPAIGN.

    The campaign filter is not cosmetic. Without it, `send --campaign X` drafted
    every approved contact regardless of which campaign they belong to, so running
    two campaigns produced a second draft to the same person under different copy
    -- 292 duplicates in one run. A contact belongs to exactly one campaign; that
    is what `contacts.campaign` is for.
    """
    return conn.execute("""
        SELECT c.*, a.name AS account_name, a.domain AS account_domain
          FROM contacts c JOIN accounts a ON a.id = c.account_id
         WHERE c.approved = 1 AND c.sendable = 1
           AND (? IS NULL OR c.campaign = ?)
           AND c.verification_status IN ('valid','catch_all','mx_only')
           AND a.validation_run = 0
           AND a.status NOT IN ('excluded','excluded_region','merged')
           AND NOT EXISTS (SELECT 1 FROM messages m
                            WHERE m.contact_id = c.id AND m.step_id = 'step1_initial'
                              AND m.state IN ('sent','sending'))
         ORDER BY c.confidence DESC LIMIT ?""", (campaign, campaign, limit)).fetchall()


_DRAFT_PROVIDERS: dict[str, object] = {}


def _draft_provider(config: Config, mailbox, provider, draft_mailbox: str | None):
    """The provider that owns a draft, which is not always the one sending.

    Cached because a batch of 17 would otherwise open 17 IMAP connections to the
    same mailbox. Falls back to the sending provider when the draft's mailbox is
    unknown or no longer in config, so a missing entry degrades to today's
    behaviour rather than raising mid-send with the message already delivered.
    """
    if not draft_mailbox or draft_mailbox == mailbox.id:
        return provider
    if draft_mailbox not in _DRAFT_PROVIDERS:
        try:
            _DRAFT_PROVIDERS[draft_mailbox] = providers.build(
                config.mailboxes.get(draft_mailbox), config.secrets())
        except Exception:
            return provider
    return _DRAFT_PROVIDERS[draft_mailbox]


def send_one(conn, config: Config, provider, mailbox, row, campaign: str,
             campaign_id: int, dry_run: bool, mode: str = "draft") -> tuple[str, str]:
    """Draft or send one message.

    `mode` is "draft" by default. A draft is not a send: it does not count
    against the daily cap, does not mark the contact contacted, and does not
    start reply tracking, because none of those things have happened until the
    operator presses send in Gmail. Conflating the two would suppress a company
    the operator never actually wrote to.

    Returns one of "done", "held" or "failed". "held" is the important one: a
    suppressed address and an already-handled row are the queue working, not
    faults, and folding them into a failure count made an unattended run cry
    wolf on every pass.
    """
    if reason := suppression.is_suppressed(conn, row["email"], row["account_name"]):
        return "held", f"suppressed: {reason}"

    step = config.step_for("step1_initial", campaign)
    cc = resolve_cc(config.cc, domain=row["account_domain"], campaign=campaign,
                    step=step.id, conn=conn)
    email = templates.render(
        config, step,
        contact={"first_name": row["first_name"], "last_name": row["last_name"],
                 "name": row["name"], "title": row["title"], "email": row["email"],
                 "personalization": row["personalization"]},
        account={"name": display_company(row["account_name"]), "domain": row["account_domain"]},
        to=row["email"], cc=cc.cc, bcc=cc.bcc,
        from_header=mailbox.from_.header(), reply_to=mailbox.reply_to, campaign=campaign)

    key = f"{row['id']}:{step.id}:{email.template_hash}"

    if dry_run:
        # The dry run reads the same row the real run will collide with. Without
        # this it reported nine clean sends for nine contacts every one of which
        # the idempotency key would have refused -- a rehearsal that could not
        # fail told us nothing about the run it was rehearsing.
        prior = conn.execute("SELECT state FROM messages WHERE idempotency_key=?",
                             (key,)).fetchone()
        if prior and not (mode == "send" and prior["state"] == "drafted"):
            return "held", f"already {prior['state']} (idempotency key exists)"
        verb = "would draft" if mode == "draft" else "would send"
        promoting = " (promoting the existing draft)" if prior else ""
        return "done", (f"{verb} to {row['email']}{promoting} (cc {cc.cc or 'none'}, "
                        f"{email.recipient_count} recipients, "
                        f"{len(email.attachments)} attachment(s))")

    in_flight = "drafting" if mode == "draft" else "sending"
    superseded: list = []
    # Commit the in-flight state BEFORE the provider call. A crash between the
    # two leaves a row to reconcile, never a second email or a second draft.
    with transaction(conn):
        if mode == "send":
            # Every draft to this person for this step is superseded by actually
            # sending, not only the one whose idempotency key matched. A template
            # edit appends a corrected draft and leaves the stale one behind, so
            # 149 contacts in this queue are holding two. Deleting just the
            # matched one leaves the other in the operator's Drafts folder,
            # reading as unsent work addressed to someone already written to.
            # Read before the promote below, so the promoted row is in the list.
            superseded = conn.execute(
                "SELECT id, provider_message_id, mailbox_id FROM messages"
                " WHERE contact_id=? AND step_id=? AND state='drafted'",
                (row["id"], step.id)).fetchall()
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages (contact_id, campaign_id, step_id, mailbox_id,"
            " state, to_addr, cc, bcc, recipient_count, subject, body_hash, template_hash,"
            " attachment_names, idempotency_key, queued_at, sending_at)"
            f" VALUES (?,?,?,?,'{in_flight}',?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], campaign_id, step.id, mailbox.id, email.to, ",".join(email.cc),
             ",".join(email.bcc), email.recipient_count, email.subject, email.body_hash,
             email.template_hash, ",".join(a.name for a in email.attachments),
             key, utcnow(), utcnow()))
        if cur.rowcount == 0:
            prior = conn.execute(
                "SELECT state, provider_message_id FROM messages WHERE idempotency_key=?",
                (key,)).fetchone()
            state = prior["state"] if prior else "queued"
            # A drafted row is the queue, not a collision. `send` exists to turn
            # reviewed drafts into sends, and the key is deliberately stable
            # across both -- so the second visit has to promote the row rather
            # than refuse it, or a drafted contact could never be sent at all.
            # Every other state still refuses: 'sent' is done, 'sending' and
            # 'drafting' belong to a run in flight, and an ambiguous 'failed' is
            # never silently retried.
            if mode != "send" or state != "drafted":
                return "held", f"already {state} (idempotency key exists)"
            conn.execute("UPDATE messages SET state='sending', sending_at=?"
                         " WHERE idempotency_key=?", (utcnow(), key))

    result = provider.create_draft(email) if mode == "draft" else provider.send(email)
    with transaction(conn):
        if result.ok and mode == "draft":
            # No cap increment, no status change, no reply tracking. Nothing has
            # been sent, and the contact has not been contacted.
            conn.execute("UPDATE messages SET state='drafted', drafted_at=?,"
                         " provider_message_id=? WHERE idempotency_key=?",
                         (utcnow(), result.message_id, key))
        elif result.ok:
            conn.execute("UPDATE messages SET state='sent', sent_at=?, provider_message_id=?,"
                         " provider_thread_id=? WHERE idempotency_key=?",
                         (utcnow(), result.message_id, result.thread_id, key))
            conn.execute("UPDATE contacts SET status='active', updated_at=? WHERE id=?",
                         (utcnow(), row["id"]))
            conn.execute("INSERT INTO mailbox_day (mailbox_id, day, messages, recipients)"
                         " VALUES (?,?,1,?) ON CONFLICT(mailbox_id, day) DO UPDATE SET"
                         " messages=messages+1, recipients=recipients+excluded.recipients",
                         (mailbox.id, utcnow()[:10], email.recipient_count))
        else:
            # An ambiguous failure is never silently retried.
            conn.execute("UPDATE messages SET state='failed', failed_at=?, error=?"
                         " WHERE idempotency_key=?", (utcnow(), result.error, key))
        log_event(conn, "info" if result.ok else "error",
                  "draft" if mode == "draft" else "send",
                  email=row["email"], ok=result.ok, error=result.error)

    verb = "drafted for" if mode == "draft" else "sent to"
    if not result.ok:
        return "failed", result.error or f"failed for {row['email']}"

    # Clear the drafts only now, after the row says 'sent'. Ordered that way on
    # purpose: a crash here leaves a draft next to a delivered message, which
    # reconcile can see and explain, whereas deleting first and dying would
    # destroy the only copy of something that never went out.
    stranded = []
    for mid, provider_message_id, draft_mailbox in superseded:
        # A draft lives in the mailbox that wrote it, which is not always the one
        # sending. Once a second mailbox joins the rotation, most of the queue's
        # drafts sit in the first one; deleting them through the sender searches
        # the wrong folder, finds nothing, and leaves the draft exactly where the
        # operator will click it -- a duplicate to someone already written to.
        owner = _draft_provider(config, mailbox, provider, draft_mailbox)
        gone, detail = owner.delete_draft(provider_message_id)
        if gone:
            # 'sent' for the row that was just delivered, 'cancelled' for the
            # superseded copies. The guard on 'drafted' is what keeps the
            # promoted row out of this.
            with transaction(conn):
                conn.execute("UPDATE messages SET state='cancelled'"
                             " WHERE id=? AND state='drafted'", (mid,))
        else:
            stranded.append(detail)
    if stranded:
        # Not a send failure -- the message went. But a leftover draft is exactly
        # the duplicate this path exists to prevent, so it is surfaced rather
        # than swallowed.
        with transaction(conn):
            log_event(conn, "warn", "draft.orphaned", email=row["email"],
                      error="; ".join(stranded))
    note = (f" -- {len(stranded)} draft(s) not removed from Drafts: {stranded[0]}"
            if stranded else "")
    return "done", f"{verb} {row['email']}{note}"


def print_drafts(conn) -> int:
    """List what is sitting in drafts. Shared by `outbound drafts` and the run
    summary, so the two can never drift into disagreeing."""
    rows = conn.execute(
        "SELECT m.id, m.to_addr, m.cc, m.subject, m.attachment_names, c.name"
        "  FROM messages m JOIN contacts c ON c.id = m.contact_id"
        " WHERE m.state = 'drafted' ORDER BY m.drafted_at").fetchall()
    if not rows:
        print("no drafts waiting")
        return 0
    print(f"\n{len(rows)} draft(s) waiting in Gmail:")
    for r in rows:
        print(f"  [{r['id']}] {r['name'][:20]:<22} {r['to_addr']:<26} "
              f"cc={r['cc'] or '(none)'}")
        print(f"       {(r['subject'] or '(no subject)')[:86]}")
        print(f"       attachments: {r['attachment_names'] or '(none)'}")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send approved contacts. One mailbox, by hand.")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--campaign", default="startup")
    ap.add_argument("--mailbox")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--send", action="store_true",
                    help="actually send. Default is to write Gmail drafts.")
    ap.add_argument("--ignore-window", action="store_true",
                    help="skip the day/time window check; caps and gates still apply")
    ap.add_argument("--no-reconcile", action="store_true",
                    help="skip the Sent-folder scan. Only safe when another run in the "
                         "same slice has already done it.")
    ap.add_argument("--config"); ap.add_argument("--db")
    args = ap.parse_args(argv)
    mode = "send" if args.send else "draft"

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr); return 2
    conn = open_db(args.db)

    enabled = config.mailboxes.enabled()
    mailbox = config.mailboxes.get(args.mailbox) if args.mailbox else (enabled[0] if enabled else None)
    if not mailbox:
        print("no enabled mailbox", file=sys.stderr); return 2

    problems = gate(conn, config, args.campaign, mailbox.id)
    if problems and not args.dry_run:
        print(f"REFUSING TO SEND -- {len(problems)} gate(s) not cleared:", file=sys.stderr)
        for p in problems:
            print(f"  - {p.splitlines()[0]}", file=sys.stderr)
        return 1

    # The window and the rolling cap govern what leaves the mailbox. A draft does
    # not leave, so neither applies -- and applying them would stop the operator
    # preparing tomorrow's batch tonight, which is the point of drafting.
    cap = config.campaign.daily_cap
    already = sent_in_window(conn, mailbox.id)
    room = max(0, cap - already)
    if mode == "send":
        ok, why = window_open(config)
        if not ok and not args.ignore_window and not args.dry_run:
            print(f"outside the sending window: {why}"); return 0
        if room == 0 and not args.dry_run:
            print(f"cap reached: {already}/{cap} recipients in the last "
                  f"{CAP_WINDOW_HOURS}h"); return 0
        # `room` is recipients; `due` takes contacts. Every message here carries
        # the CCs, so divide by the widest a message can be rather than sending
        # 17 three-recipient messages into 30 recipients of headroom.
        room = max(1, room // max(1, config.cc.max_recipients_per_message()))
    else:
        room = args.limit

    campaign_id = get_or_create_campaign(conn, args.campaign)
    rows = due(conn, campaign_id, min(args.limit, room or args.limit), args.campaign)
    if not rows:
        print("nothing due: no approved, verified, unsent contacts"); return 0

    provider = providers.build(mailbox, config.secrets())

    # Notice what has already gone before deciding what to queue. Without this
    # the operator has to announce each send, and skipping that silently
    # re-queues people who were already written to.
    #
    # It is also the most expensive thing here -- it scans Gmail folders, and on
    # 2026-09-02 one pass took 19 minutes against a mailbox this size. An hourly
    # slice that runs it once per campaign spends most of its watchdog budget
    # before the first send, which is how runs were dying with 1 of 10 sent.
    # Once per slice is enough: nothing between two campaigns in the same run
    # changes what is sitting in the Sent folder.
    if not args.dry_run and not args.no_reconcile:
        from .reconcile import reconcile

        try:
            with transaction(conn):
                rec = reconcile(conn, config, provider)
            if rec.marked:
                print(f"noticed {len(rec.marked)} draft(s) you already sent:")
                for _mid, to in rec.marked[:8]:
                    print(f"    {to}")
                print()
            elif rec.error:
                print(f"could not check your Sent folder ({rec.error}); "
                      f"drafts already sent may be re-queued\n")
        except Exception as exc:
            print(f"sent-folder check failed ({type(exc).__name__}); continuing\n")

    if not args.dry_run:
        auth_ok, detail = provider.verify_auth()
        if not auth_ok:
            print(f"mailbox not authorised: {detail}", file=sys.stderr); return 2

    verb = "to draft" if mode == "draft" else "to send"
    print(f"{'DRY RUN: ' if args.dry_run else ''}{len(rows)} {verb} from {mailbox.id}"
          f"{'' if mode == 'draft' else f' ({already}/{cap} recipients in the last {CAP_WINDOW_HOURS}h)'}\n")
    done = failed = held = 0
    marks = {"done": "ok  ", "held": "hold", "failed": "FAIL"}
    delay = config.campaign.inter_send_delay
    for i, row in enumerate(rows):
        # Re-read the cap between sends rather than trusting the estimate made
        # before the batch. The pre-filter divides a recipient budget by the
        # worst-case message width, which is an estimate; this is the ledger.
        if mode == "send" and not args.dry_run:
            used = sent_in_window(conn, mailbox.id)
            if used >= cap:
                print(f"  stop  cap reached mid-batch: {used}/{cap} recipients "
                      f"in the last {CAP_WINDOW_HOURS}h")
                break
        outcome, detail = send_one(conn, config, provider, mailbox, row, args.campaign,
                                   campaign_id, args.dry_run, mode=mode)
        print(f"  {marks[outcome]} {row['name'][:22]:<22} {detail[:80]}")
        done += outcome == "done"
        failed += outcome == "failed"
        held += outcome == "held"
        # Pacing exists to look human to a receiving server. Nothing is reaching
        # one in draft mode, and a held row never reached one either.
        if (mode == "send" and outcome == "done" and not args.dry_run
                and i < len(rows) - 1):
            time.sleep(random.uniform(delay.min_seconds, delay.max_seconds))
    past = "drafted" if mode == "draft" else "sent"
    print(f"\n{done} {('would be ' + past) if args.dry_run else past}, {failed} failed"
          + (f", {held} held" if held else ""))
    if mode == "draft" and done and not args.dry_run:
        # The whole draft list, printed here rather than left behind a command
        # the operator has to remember exists. `mark-sent` is the one manual
        # step in the flow, so it goes on screen at the moment it becomes due.
        print_drafts(conn)
        print(f"\nNothing above counts as contacted yet:\n"
              f"  - the daily cap is untouched ({already}/{cap} used today)\n"
              f"  - these contacts are not marked active\n"
              f"  - reply tracking and company suppression have not started\n"
              f"\nOpen Gmail, read them, send the ones you want. That is the whole\n"
              f"remaining step -- the next run checks your Sent folder and marks\n"
              f"them itself.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
