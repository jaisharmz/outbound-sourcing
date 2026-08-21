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
from datetime import date, datetime

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


def sent_today(conn, mailbox_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(recipients),0) FROM mailbox_day WHERE mailbox_id=? AND day=?",
        (mailbox_id, utcnow()[:10])).fetchone()[0]


def gate(conn, config: Config, campaign: str, mailbox_id: str) -> list[str]:
    """Everything that must be true before a single email leaves."""
    problems = list(config.preflight("campaign", campaign=campaign))

    from .check_links import gate as link_gate
    problems.extend(link_gate(conn, config, campaign))

    h = templates.template_hash(config, campaign)
    row = conn.execute(
        "SELECT template_hash, sent_at FROM test_sends WHERE mailbox_id=? AND ok=1"
        " ORDER BY id DESC LIMIT 1", (mailbox_id,)).fetchone()
    if not row:
        problems.append(f"mailbox {mailbox_id!r} has never passed a test send. "
                        f"Run: outbound test-email --mailbox {mailbox_id}")
    elif row["template_hash"] != h:
        problems.append(
            f"templates changed since the last passing test send for {mailbox_id!r} "
            f"({row['template_hash']} then, {h} now). Re-run the test send.")
    return problems


def due(conn, campaign_id: int, limit: int) -> list:
    return conn.execute("""
        SELECT c.*, a.name AS account_name, a.domain AS account_domain
          FROM contacts c JOIN accounts a ON a.id = c.account_id
         WHERE c.approved = 1 AND c.sendable = 1
           AND c.verification_status IN ('valid','catch_all','mx_only')
           AND a.validation_run = 0
           AND a.status NOT IN ('excluded','excluded_region','merged')
           AND NOT EXISTS (SELECT 1 FROM messages m
                            WHERE m.contact_id = c.id AND m.step_id = 'step1_initial'
                              AND m.state IN ('sent','sending'))
         ORDER BY c.confidence DESC LIMIT ?""", (limit,)).fetchall()


def send_one(conn, config: Config, provider, mailbox, row, campaign: str,
             campaign_id: int, dry_run: bool, mode: str = "draft") -> tuple[bool, str]:
    """Draft or send one message.

    `mode` is "draft" by default. A draft is not a send: it does not count
    against the daily cap, does not mark the contact contacted, and does not
    start reply tracking, because none of those things have happened until the
    operator presses send in Gmail. Conflating the two would suppress a company
    the operator never actually wrote to.
    """
    if reason := suppression.is_suppressed(conn, row["email"], row["account_name"]):
        return False, f"suppressed: {reason}"

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

    if dry_run:
        verb = "would draft" if mode == "draft" else "would send"
        return True, (f"{verb} to {row['email']} (cc {cc.cc or 'none'}, "
                      f"{email.recipient_count} recipients, "
                      f"{len(email.attachments)} attachment(s))")

    key = f"{row['id']}:{step.id}:{email.template_hash}"
    in_flight = "drafting" if mode == "draft" else "sending"
    # Commit the in-flight state BEFORE the provider call. A crash between the
    # two leaves a row to reconcile, never a second email or a second draft.
    with transaction(conn):
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
            return False, "already queued, drafted or sent (idempotency key exists)"

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
    return result.ok, (result.error or f"{verb} {row['email']}")


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

    # The window and the daily cap govern what leaves the mailbox. A draft does
    # not leave, so neither applies -- and applying them would stop the operator
    # preparing tomorrow's batch tonight, which is the point of drafting.
    cap = config.campaign.daily_cap
    already = sent_today(conn, mailbox.id)
    room = max(0, cap - already)
    if mode == "send":
        ok, why = window_open(config)
        if not ok and not args.ignore_window and not args.dry_run:
            print(f"outside the sending window: {why}"); return 0
        if room == 0 and not args.dry_run:
            print(f"daily cap reached: {already}/{cap} recipients today"); return 0
    else:
        room = args.limit

    campaign_id = get_or_create_campaign(conn, args.campaign)
    rows = due(conn, campaign_id, min(args.limit, room or args.limit))
    if not rows:
        print("nothing due: no approved, verified, unsent contacts"); return 0

    provider = providers.build(mailbox, config.secrets())
    if not args.dry_run:
        auth_ok, detail = provider.verify_auth()
        if not auth_ok:
            print(f"mailbox not authorised: {detail}", file=sys.stderr); return 2

    verb = "to draft" if mode == "draft" else "to send"
    print(f"{'DRY RUN: ' if args.dry_run else ''}{len(rows)} {verb} from {mailbox.id}"
          f"{'' if mode == 'draft' else f' ({already}/{cap} recipients used today)'}\n")
    done = failed = 0
    delay = config.campaign.inter_send_delay
    for i, row in enumerate(rows):
        ok, detail = send_one(conn, config, provider, mailbox, row, args.campaign,
                              campaign_id, args.dry_run, mode=mode)
        print(f"  {'ok  ' if ok else 'FAIL'} {row['name'][:22]:<22} {detail[:80]}")
        done += ok; failed += not ok
        # Pacing exists to look human to a receiving server. Nothing is reaching
        # one in draft mode, so there is nothing to pace.
        if mode == "send" and not args.dry_run and i < len(rows) - 1:
            time.sleep(random.uniform(delay.min_seconds, delay.max_seconds))
    past = "drafted" if mode == "draft" else "sent"
    print(f"\n{done} {('would be ' + past) if args.dry_run else past}, {failed} failed")
    if mode == "draft" and done and not args.dry_run:
        print(f"\nReview them in Gmail and send by hand. Nothing counts as contacted "
              f"until then:\n"
              f"  - the daily cap is untouched ({already}/{cap} used today)\n"
              f"  - these contacts are still 'new', not 'active'\n"
              f"  - reply tracking and company suppression have not started\n"
              f"After sending, run: outbound mark-sent --all")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
