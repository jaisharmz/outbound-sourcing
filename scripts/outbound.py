"""`outbound` CLI. SKILL.md orchestrates these commands rather than reimplementing them.

    python -m scripts.outbound --help
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import typer

from .cc import resolve as resolve_cc
from .config import Config, load_config
from .db import open_db, transaction, utcnow
from .errors import ConfigError
from .ingest_candidates import ingest
from .normalize import display_company, registrable_domain
from . import providers, suppression, templates

app = typer.Typer(add_completion=False, help="Outbound sourcing: discovery, review, send.")
db_app = typer.Typer(help="Database maintenance.")
suppress_app = typer.Typer(help="Permanent global suppression.")
app.add_typer(db_app, name="db")
app.add_typer(suppress_app, name="suppress")

ROOT = Path(__file__).resolve().parent.parent


def _config(path: Optional[str]) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2)


def _err(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)


# ------------------------------------------------------------------ config


@app.command("validate-config")
def validate_config(config: Optional[str] = typer.Option(None, "--config")):
    """Load and check every config file, including cross-file references."""
    cfg = _config(config)
    typer.secho(f"config OK: {cfg.root}", fg=typer.colors.GREEN)
    typer.echo(f"  persona:     {cfg.persona.name} ({cfg.persona.org})")
    typer.echo(f"  campaign:    {cfg.campaign.name}, step1 variant = {cfg.campaign.step1_variant}")
    typer.echo(f"  mailboxes:   {len(cfg.mailboxes.mailboxes)} defined, "
               f"{len(cfg.mailboxes.enabled())} enabled")
    typer.echo(f"  sequence:    {' -> '.join(s.id for s in cfg.sequence.steps)}")
    typer.echo(f"  dorks:       {len(cfg.dorks)} search seeds")
    typer.echo(f"  templates:   hash {templates.template_hash(cfg)}")
    if cfg.campaign.verification.catch_all_share_is_placeholder:
        typer.secho(
            "  note: verification.catch_all_daily_share is still flagged a placeholder. "
            "Revise it from observed bounce rate after real sends.",
            fg=typer.colors.YELLOW,
        )


# ------------------------------------------------------------------ db


@db_app.command("migrate")
def db_migrate(db: Optional[str] = typer.Option(None, "--db")):
    """Apply pending migrations. Idempotent."""
    conn = open_db(db)
    version = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    typer.secho(f"database ready (schema {version['version'] if version else 'empty'})",
                fg=typer.colors.GREEN)


@db_app.command("stats")
def db_stats(db: Optional[str] = typer.Option(None, "--db")):
    """Row counts, for a quick sanity check."""
    conn = open_db(db)
    for table in ("accounts", "contacts", "evidence", "messages", "replies",
                  "suppression", "test_sends"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        typer.echo(f"  {table:<14} {n}")


# ------------------------------------------------------------------ ingest


@app.command("ingest")
def ingest_cmd(
    directory: Optional[str] = typer.Option(None, "--dir"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Validate candidate JSON and load it into SQLite."""
    cfg = _config(config)
    conn = open_db(db)
    suppression.load_csv(conn, cfg.root / "suppression.csv")
    target = Path(directory) if directory else ROOT / cfg.campaign.discovery.candidates_dir
    if not target.exists():
        _err(f"candidates directory not found: {target}")
        raise typer.Exit(2)
    report = ingest(conn, cfg, target, dry_run=dry_run)
    if dry_run:
        typer.secho("dry run -- nothing was written", fg=typer.colors.YELLOW)
    typer.echo(report.summary())
    if report.files_rejected:
        raise typer.Exit(1)


# ------------------------------------------------------------------ render


@app.command("render")
def render_cmd(
    step: str = typer.Option(..., "--step"),
    email: Optional[str] = typer.Option(None, "--email", help="contact email in the DB"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    campaign: Optional[str] = typer.Option(None, "--campaign"),
):
    """Render one email exactly as it would send, and print it."""
    cfg = _config(config)
    conn = open_db(db)
    if email:
        row = conn.execute(
            "SELECT c.*, a.name AS account_name, a.domain AS account_domain"
            " FROM contacts c JOIN accounts a ON a.id = c.account_id WHERE c.email = ?",
            (email.lower(),),
        ).fetchone()
        if not row:
            _err(f"no contact with email {email!r}")
            raise typer.Exit(2)
        contact, account = _row_to_context(row)
    else:
        contact, account = _fixture_contact()

    rendered = _render_for(cfg, conn, step, contact, account, campaign)
    typer.echo(rendered.preview())


def _row_to_context(row: sqlite3.Row) -> tuple[dict, dict]:
    contact = {
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "name": row["name"],
        "title": row["title"],
        "email": row["email"],
        "personalization": row["personalization"],
    }
    account = {
        "name": display_company(row["account_name"]),
        "legal_name": row["account_name"],
        "domain": row["account_domain"],
    }
    return contact, account


def _fixture_contact() -> tuple[dict, dict]:
    """A stand-in contact for test sends and template previews."""
    return (
        {
            "first_name": "Alex",
            "last_name": "Rivera",
            "name": "Alex Rivera",
            "title": "Research Scientist",
            "email": "alex@example-fixture.test",
            "personalization": None,
        },
        {"name": "Example Fixture Lab", "domain": "example-fixture.test"},
    )


def _render_for(cfg: Config, conn, step_id: str, contact: dict, account: dict,
                campaign: Optional[str], mailbox_id: Optional[str] = None):
    step = cfg.sequence.get(step_id)
    mailbox = cfg.mailboxes.get(mailbox_id) if mailbox_id else cfg.mailboxes.enabled()[0]
    cc = resolve_cc(
        cfg.cc,
        domain=account.get("domain"),
        campaign=campaign or cfg.campaign.name,
        step=step_id,
        conn=conn,
    )
    if cc.suppressed:
        typer.secho(f"  (dropped suppressed cc/bcc: {', '.join(cc.suppressed)})",
                    fg=typer.colors.YELLOW)
    return templates.render(
        cfg, step,
        contact=contact, account=account, to=contact["email"],
        cc=cc.cc, bcc=cc.bcc,
        from_header=mailbox.from_.header(), reply_to=mailbox.reply_to,
    )


# ------------------------------------------------------------------ cc


@app.command("cc-resolve")
def cc_resolve(
    domain: Optional[str] = typer.Option(None, "--domain"),
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    step: Optional[str] = typer.Option(None, "--step"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Show which CC/BCC rule wins for a given send, and why."""
    cfg = _config(config)
    conn = open_db(db)
    r = resolve_cc(cfg.cc, domain=domain, campaign=campaign, step=step, conn=conn)
    typer.echo(f"cc:  {r.cc or '(none)'}   <- {r.cc_source}")
    typer.echo(f"bcc: {r.bcc or '(none)'}  <- {r.bcc_source}")
    if r.suppressed:
        typer.secho(f"suppressed and removed: {r.suppressed}", fg=typer.colors.YELLOW)
    typer.echo(f"extra recipients counted against the daily cap: {r.recipient_count}")


# ------------------------------------------------------------------ suppress


@suppress_app.command("add")
def suppress_add(
    value: str = typer.Argument(...),
    kind: str = typer.Option("email", "--kind", help="email | domain | company"),
    reason: str = typer.Option("manual", "--reason"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Suppress an address, domain, or company. Permanent and global."""
    cfg = _config(config)
    conn = open_db(db)
    with transaction(conn):
        added = suppression.add(conn, kind, value, reason,
                                csv_path=cfg.root / "suppression.csv")
        if kind == "company":
            suppression.suppress_company(conn, value, reason,
                                         csv_path=cfg.root / "suppression.csv")
    typer.secho("suppressed" if added else "already suppressed", fg=typer.colors.GREEN)


@suppress_app.command("list")
def suppress_list(db: Optional[str] = typer.Option(None, "--db")):
    conn = open_db(db)
    rows = conn.execute(
        "SELECT kind, value, reason, created_at FROM suppression ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        typer.echo("(suppression list is empty)")
        return
    for r in rows:
        typer.echo(f"  {r['kind']:<8} {r['value']:<40} {r['reason']}  {r['created_at']}")


# ------------------------------------------------------------------ demo


@app.command("demo")
def demo(
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    fixtures: Optional[str] = typer.Option(None, "--fixtures"),
):
    """End-to-end on fixture contacts through the console mailbox. No network."""
    cfg = _config(config)
    conn = open_db(db)
    suppression.load_csv(conn, cfg.root / "suppression.csv")

    src = Path(fixtures) if fixtures else ROOT / "tests" / "fixtures" / "candidates"
    typer.secho(f"\n[1/3] ingesting {src}", fg=typer.colors.CYAN)
    report = ingest(conn, cfg, src, source="fixture")
    typer.echo(report.summary())

    typer.secho("\n[2/3] contacts in the database", fg=typer.colors.CYAN)
    rows = conn.execute(
        "SELECT c.*, a.name AS account_name, a.domain AS account_domain, a.status AS account_status"
        " FROM contacts c JOIN accounts a ON a.id = c.account_id ORDER BY c.id"
    ).fetchall()
    for r in rows:
        n_ev = conn.execute(
            "SELECT COUNT(*) FROM evidence WHERE contact_id = ?", (r["id"],)
        ).fetchone()[0]
        pz = "grounded" if r["personalization"] else "null -> template falls back"
        typer.echo(f"  {r['name']:<16} {r['title']:<20} {r['email']:<32} "
                   f"evidence={n_ev}  personalization={pz}")

    mailbox = cfg.mailboxes.get("console")
    provider = providers.build(mailbox, cfg.secrets())
    step_id = cfg.sequence.steps[0].id

    typer.secho(f"\n[3/3] rendering and sending step {step_id} via the console mailbox",
                fg=typer.colors.CYAN)
    for r in rows:
        contact, account = _row_to_context(r)
        rendered = _render_for(cfg, conn, step_id, contact, account, cfg.campaign.name,
                               mailbox_id="console")
        result = provider.send(rendered)
        with transaction(conn):
            _record(conn, cfg, r["id"], step_id, mailbox.id, rendered, result)

    sent = conn.execute("SELECT COUNT(*) FROM messages WHERE state = 'sent'").fetchone()[0]
    typer.secho(f"\ndone: {sent} message(s) recorded as sent, zero network calls",
                fg=typer.colors.GREEN)


def _record(conn, cfg: Config, contact_id: int, step_id: str, mailbox_id: str,
            rendered, result) -> None:
    """Write the message row the way the real sender will: commit before, update after."""
    campaign_id = _campaign_id(conn, cfg.campaign.name)
    key = f"{contact_id}:{step_id}:{rendered.template_hash}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages (contact_id, campaign_id, step_id, mailbox_id, state,"
        " to_addr, cc, bcc, recipient_count, subject, body_hash, template_hash, variant,"
        " attachment_names, idempotency_key, queued_at, sending_at)"
        " VALUES (?,?,?,?,'sending',?,?,?,?,?,?,?,?,?,?,?,?)",
        (contact_id, campaign_id, step_id, mailbox_id, rendered.to,
         ",".join(rendered.cc), ",".join(rendered.bcc), rendered.recipient_count,
         rendered.subject, rendered.body_hash, rendered.template_hash, rendered.variant,
         ",".join(a.name for a in rendered.attachments), key, utcnow(), utcnow()),
    )
    if cur.rowcount == 0:
        return
    conn.execute(
        "UPDATE messages SET state = 'sent', sent_at = ?, provider_message_id = ?,"
        " provider_thread_id = ? WHERE idempotency_key = ?",
        (utcnow(), result.message_id, result.thread_id, key),
    )
    conn.execute(
        "INSERT INTO mailbox_day (mailbox_id, day, messages, recipients) VALUES (?,?,1,?)"
        " ON CONFLICT(mailbox_id, day) DO UPDATE SET messages = messages + 1,"
        " recipients = recipients + excluded.recipients",
        (mailbox_id, utcnow()[:10], rendered.recipient_count),
    )


def _campaign_id(conn, name: str) -> int:
    from .db import get_or_create_campaign
    return get_or_create_campaign(conn, name)


if __name__ == "__main__":
    app()
