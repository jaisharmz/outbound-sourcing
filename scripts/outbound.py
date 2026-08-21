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
from .config import Config, load_config, wire_size
from .db import open_db, transaction, utcnow
from .errors import ConfigError
from .discover_companies import main as discover_main
from .ingest_candidates import ingest
from .normalize import display_company, registrable_domain
from . import prefilter as prefilter_mod
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
    typer.echo(f"  campaign:    {cfg.campaign.name}")
    enabled = cfg.mailboxes.enabled()
    typer.echo(f"  mailbox:     {enabled[0].id if enabled else '(none enabled)'}"
               + (f"  From {enabled[0].from_.address}, Reply-To {enabled[0].reply_to}"
                  if enabled else ""))
    typer.echo(f"  sequence:    {' -> '.join(s.id for s in cfg.sequence.steps)}")
    typer.echo(f"  dorks:       {len(cfg.dorks)} search seeds")
    typer.echo(f"  templates:   hash {templates.template_hash(cfg)}")
    typer.echo(f"  attachments: {cfg.campaign.max_attachment_bytes/1_000_000:.2f} MB max at "
               f"load (test sends), {cfg.campaign.campaign_max_attachment_bytes/1_000_000:.2f} "
               f"MB gate for campaign start")
    typer.echo(f"  daily cap:   {cfg.campaign.daily_cap} sends")

    blockers = cfg.preflight("campaign")
    if blockers:
        typer.secho(f"\nCAMPAIGN BLOCKERS ({len(blockers)}) -- config is structurally valid, "
                    f"but no campaign may start until these are resolved:",
                    fg=typer.colors.RED)
        for b in blockers:
            typer.echo(f"  - {b}")
        typer.secho("\nTest sends to your own address still work; they render the "
                    "placeholder so you can see exactly what would ship.",
                    fg=typer.colors.YELLOW)
        raise typer.Exit(1)


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


# ------------------------------------------------------------------ discover


@app.command("discover")
def discover_cmd(
    mode: str = typer.Option(..., "--mode", help="list | vc | industry"),
    run: Optional[str] = typer.Option(None, "--run", help="industry-research run dir"),
    fund: Optional[str] = typer.Option(None, "--fund", help="fund name from funds.yaml"),
    file: Optional[str] = typer.Option(None, "--file", help="company list file"),
    tier: Optional[str] = typer.Option(None, "--tier"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Find companies and load them into the accounts table."""
    argv = ["--mode", mode]
    for flag, value in (("--run", run), ("--fund", fund), ("--file", file), ("--tier", tier),
                        ("--config", config), ("--db", db)):
        if value:
            argv += [flag, value]
    if dry_run:
        argv.append("--dry-run")
    raise typer.Exit(discover_main(argv))


@app.command("accounts")
def accounts_cmd(
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    status: Optional[str] = typer.Option(None, "--status"),
    needs_domain: bool = typer.Option(False, "--needs-domain"),
    needs_triage: bool = typer.Option(False, "--needs-triage",
                                      help="imported without a tier, so cannot enroll"),
    db: Optional[str] = typer.Option(None, "--db"),
    limit: int = typer.Option(40, "--limit"),
):
    """List accounts. --needs-domain shows the ones blocking people discovery."""
    conn = open_db(db)
    where, params = [], []
    if campaign:
        where.append("campaign = ?"); params.append(campaign)
    if status:
        where.append("status = ?"); params.append(status)
    if needs_domain:
        where.append("domain IS NULL AND status != 'excluded'")
    if needs_triage:
        where.append("tier IS NULL AND status != 'excluded'")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT name, tier, campaign, domain, domain_confidence, status FROM accounts"
        f"{clause} ORDER BY campaign, name LIMIT ?", (*params, limit)
    ).fetchall()
    if not rows:
        typer.echo("(no matching accounts)")
        return
    for r in rows:
        typer.echo(f"  {str(r['campaign'] or '-'):<13} {str(r['status']):<10} "
                   f"{str(r['domain'] or '(no domain)'):<26} "
                   f"{str(r['domain_confidence']):<11} {r['name']}")
    total = conn.execute(f"SELECT COUNT(*) FROM accounts{clause}", tuple(params)).fetchone()[0]
    typer.echo(f"\n  {len(rows)} shown of {total}")


# ------------------------------------------------------------------ homepages


@app.command("homepages")
def homepages_cmd(
    fund: Optional[str] = typer.Option(None, "--fund"),
    refetch: bool = typer.Option(False, "--refetch", help="include already-fetched rows"),
    workers: int = typer.Option(8, "--workers"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Fetch each account's homepage and store what the company says about itself.

    Better evidence than an investor's blurb, and immune to the name collisions a
    search hits, because the domain is already known. Stored for every account so
    stage 0 can be re-judged later without re-fetching.
    """
    from . import homepages as hp

    conn = open_db(db)
    where = ["domain IS NOT NULL", "status != 'excluded'"]
    params: list = []
    if fund:
        where.append("fund = ?")
        params.append(fund)
    if not refetch:
        where.append("homepage_fetch_status IS NULL")
    sql = f"SELECT id, domain FROM accounts WHERE {' AND '.join(where)} ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [(r["id"], r["domain"]) for r in conn.execute(sql, tuple(params))]
    if not rows:
        typer.echo("(nothing to fetch)")
        return

    typer.echo(f"fetching {len(rows)} homepages with {workers} workers...")
    results = hp.fetch_many(rows, workers=workers)
    counts: dict[str, int] = {}
    with transaction(conn):
        for aid, res in results.items():
            counts[res.status] = counts.get(res.status, 0) + 1
            conn.execute(
                "UPDATE accounts SET homepage_url = ?, homepage_text = ?,"
                " homepage_fetch_status = ?, homepage_fetched_at = ? WHERE id = ?",
                (res.url, res.text or None, res.status, utcnow(), aid),
            )
    total = sum(counts.values())
    typer.echo(f"\n{total} fetched:")
    for k in ("ok", "js_shell", "holding", "blocked", "dead"):
        if counts.get(k):
            typer.echo(f"  {k:<9} {counts[k]:>4}  ({100*counts[k]/total:.0f}%)")
    unreachable = total - counts.get("ok", 0)
    typer.secho(
        f"\n  {unreachable} produced no usable text. Those are `unknown` for stage 0, "
        f"never `fail` -- a site that did not render is not a company without an ML team.",
        fg=typer.colors.YELLOW,
    )


# ------------------------------------------------------------------ harvest


@app.command("harvest-github")
def harvest_github_cmd(
    prefilter: str = typer.Option("pass_builds", "--prefilter"),
    fund: Optional[str] = typer.Option(None, "--fund"),
    limit: Optional[int] = typer.Option(None, "--limit"),
    repos: int = typer.Option(4, "--repos"),
    config_path: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Harvest observed addresses from public commits and infer domain patterns.

    Deterministic: no model, no judgment. The addresses matter less than the
    pattern they establish -- one confirmed convention turns every name found
    elsewhere into a candidate address.
    """
    from . import github_harvest as gh

    cfg = _config(config_path)
    conn = open_db(db)
    token = cfg.secret("GITHUB_TOKEN")
    if not token:
        typer.secho(
            "GITHUB_TOKEN is not set in config/secrets.env. Unauthenticated GitHub allows "
            "60 requests an hour, which is not enough to tell 'no public repos' apart from "
            "'throttled' across a real roster. Create a fine-grained token with no scopes "
            "selected -- public read is the default -- at github.com/settings/tokens.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    where, params = ["prefilter = ?", "domain IS NOT NULL", "status != 'excluded'"], [prefilter]
    if fund:
        where.append("fund = ?")
        params.append(fund)
    sql = f"SELECT id, name, domain FROM accounts WHERE {' AND '.join(where)} ORDER BY name"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, tuple(params)).fetchall()

    client = gh.Client(token=token)
    typer.echo(f"harvesting {len(rows)} domains...\n")
    results, statuses = [], {}
    for r in rows:
        res = gh.harvest_domain(client, r["name"], r["domain"], repos=repos)
        results.append((r["id"], res))
        statuses[res.status] = statuses.get(res.status, 0) + 1
        if res.addresses:
            pattern, conf, _ = gh.infer_pattern(res.addresses)
            typer.echo(f"  {r['name'][:24]:<24} {len(res.fresh)} fresh / {len(res.stale)} stale"
                       f"   pattern={pattern or '-'}")

    with transaction(conn):
        for account_id, res in results:
            for email, (name, when) in res.addresses.items():
                fresh = email in res.fresh
                conn.execute(
                    "INSERT OR IGNORE INTO known_people (account_id, name, role, provenance,"
                    " source_url, email, email_observed_at, name_quality, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (account_id, name or email.split("@")[0], "commit author",
                     "github_commit" if fresh else "github_commit_stale",
                     f"https://github.com/{res.org}", email, when,
                     gh.name_quality(name or "", res.domain), utcnow()),
                )
            pattern, conf, used = gh.infer_pattern(res.addresses)
            conn.execute(
                "UPDATE accounts SET email_pattern = ?, email_pattern_confidence = ?,"
                " email_pattern_evidence = ?, email_pattern_samples = ?,"
                " newest_commit_at = ?, github_org = ?, github_archived_repos = ?"
                " WHERE id = ?",
                (pattern, conf if pattern else None,
                 ",".join(sorted(used))[:500] if pattern else None,
                 len(used) if pattern else None,
                 res.newest_commit_at, res.org, res.archived_repos, account_id),
            )

    typer.echo("\noutcomes:")
    for k in sorted(statuses):
        typer.echo(f"  {k:<20} {statuses[k]}")
    if client.limiter.throttled:
        typer.secho("\n  RATE LIMITED during this run -- some 'no_*' results above may be "
                    "incomplete. Re-run to fill them in.", fg=typer.colors.YELLOW)
    typer.echo(f"\n  {client.calls} API calls, {client.limiter.remaining} remaining")


# ------------------------------------------------------------------ prefilter


@app.command("prefilter")
def prefilter_cmd(
    fund: Optional[str] = typer.Option(None, "--fund"),
    rerun: bool = typer.Option(False, "--rerun", help="re-judge everything, not just new"),
    export_batch: Optional[str] = typer.Option(None, "--export-batch", help="write JSON"),
    import_verdicts: Optional[str] = typer.Option(None, "--import-verdicts"),
    limit: int = typer.Option(200, "--limit"),
    config_path: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Stage 0: decide which accounts are worth a full research budget.

    Verdicts are stored with the ruleset and the text judged, and the fund
    payload is cached, so stage 0 can be re-run with better rules without
    re-fetching. `fail` is a verdict, not a deletion.
    """
    conn = open_db(db)

    if export_batch:
        batch = prefilter_mod.export_batch(conn, fund=fund, limit=limit)
        Path(export_batch).write_text(json.dumps(batch, indent=2))
        typer.echo(f"wrote {len(batch)} companies to {export_batch}")
        typer.echo("\nClassify them against this brief, then re-import:\n")
        typer.echo(prefilter_mod.CLASSIFY_BRIEF)
        return

    if import_verdicts:
        try:
            payload = json.loads(Path(import_verdicts).read_text())
        except json.JSONDecodeError as exc:
            _err(f"{import_verdicts}: not valid JSON -- {exc}")
            raise typer.Exit(2)
        routes = _config(config_path).campaigns.depth_routes()
        try:
            with transaction(conn):
                counts = prefilter_mod.import_verdicts(conn, payload, depth_routes=routes)
        except ValueError as exc:
            _err(str(exc))
            raise typer.Exit(2)
        typer.echo(prefilter_mod.summary(counts))
        return

    with transaction(conn):
        counts = prefilter_mod.apply(conn, fund=fund, only_unjudged=not rerun)
    typer.echo(prefilter_mod.summary(counts))
    typer.secho(
        "\nkeywords_v1 was measured against a hand-checked sample and dropped roughly a "
        "third of real targets. Use --export-batch for the classifier pass before "
        "spending research budget on this verdict.",
        fg=typer.colors.YELLOW,
    )


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


# ------------------------------------------------------------------ auth


@app.command("auth")
def auth_cmd(
    mailbox: str = typer.Option(..., "--mailbox"),
    config: Optional[str] = typer.Option(None, "--config"),
):
    """Run the OAuth flow for one mailbox and say precisely what happened.

    The failure modes matter and are not interchangeable: a declined consent is
    fixable by retrying, while an admin policy that forbids the scope is not
    fixable in code at all.
    """
    cfg = _config(config)
    mb = cfg.mailboxes.get(mailbox)
    if mb.provider == "console":
        typer.secho("console mailbox needs no authorization", fg=typer.colors.GREEN)
        return

    provider = providers.build(mb, cfg.secrets())
    typer.echo(f"authorizing {mb.id} ({mb.from_.address}) for scopes:")
    from .providers.gmail import SCOPES

    for scope in SCOPES:
        typer.echo(f"  {scope}")
    typer.echo("a browser window will open; complete consent there\n")

    try:
        ok, message = provider.authorize()
    except Exception as exc:
        _err(str(exc))
        raise typer.Exit(2)

    if ok:
        typer.secho(message, fg=typer.colors.GREEN)
        typer.echo(f"token stored at state/tokens/{mb.id}.json (mode 600)")
    else:
        typer.secho(message, fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


# ------------------------------------------------------------------ test send


@app.command("test-email")
def test_email(
    mailbox: Optional[str] = typer.Option(None, "--mailbox"),
    step: str = typer.Option("step1_initial", "--step"),
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    to: Optional[str] = typer.Option(None, "--to", help="override test_recipient"),
    force: bool = typer.Option(False, "--force",
                               help="send to an address outside test_send_allowlist"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    wait: int = typer.Option(30, "--wait", help="seconds to wait for the delivered copy"),
):
    """Send a fully real email to test_recipient and print what actually went out.

    Real template, real persona, real attachments, real CC list, real footer,
    real headers. This is the gate: the scheduler refuses to send from a mailbox
    that has never passed one, and refuses to start a campaign whose templates
    changed since the last one.
    """
    cfg = _config(config)
    conn = open_db(db)

    blockers = cfg.preflight("campaign")
    if blockers:
        typer.secho(f"note: {len(blockers)} campaign blocker(s) are unresolved. The test "
                    f"send still goes out so you can see exactly what would ship:",
                    fg=typer.colors.YELLOW)
        for b in blockers:
            typer.echo(f"  - {b.splitlines()[0]}")
        typer.echo("")

    targets = [mailbox] if mailbox else [m.id for m in cfg.mailboxes.enabled()]
    if not targets:
        _err("no mailbox enabled in mailboxes.yaml")
        raise typer.Exit(2)

    failures = 0
    for mailbox_id in targets:
        if not _one_test_send(cfg, conn, mailbox_id, step, campaign, wait, to, force):
            failures += 1
    if failures:
        raise typer.Exit(1)


def _one_test_send(cfg: Config, conn, mailbox_id: str, step_id: str,
                   campaign: Optional[str], wait: int, to_override: str | None = None,
                   force: bool = False) -> bool:
    mb = cfg.mailboxes.get(mailbox_id)

    # Check credentials before rendering. With --all-mailboxes an unauthorized
    # pool would otherwise render and fail once per mailbox before saying why.
    provider = providers.build(mb, cfg.secrets())
    ok, detail = provider.verify_auth()
    if not ok:
        typer.secho(f"\n=== test send: mailbox {mailbox_id} ===", fg=typer.colors.CYAN)
        typer.secho(f"  NOT AUTHORIZED: {detail}", fg=typer.colors.RED)
        conn.execute(
            "INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash, to_addr,"
            " ok, error, sent_at) VALUES (?,?,?,?,?,0,?,?)",
            (mailbox_id, step_id, campaign or cfg.campaign.name,
             templates.template_hash(cfg), cfg.campaign.test_recipient, detail, utcnow()),
        )
        return False

    contact, account = _fixture_contact()
    to = (to_override or cfg.campaign.test_recipient).strip().lower()
    contact["email"] = to

    allowed, forced = _authorize_test_recipient(cfg, conn, mailbox_id, step_id, campaign, to, force)
    if not allowed:
        return False

    rendered = _render_for(cfg, conn, step_id, contact, account, campaign,
                           mailbox_id=mailbox_id)
    rendered.to = to

    typer.secho(f"\n=== test send: mailbox {mailbox_id}, step {step_id} ===",
                fg=typer.colors.CYAN)
    typer.echo(f"  from:        {rendered.from_header}")
    typer.echo(f"  reply-to:    {rendered.reply_to or '(none)'}")
    typer.echo(f"  to:          {rendered.to}")
    typer.echo(f"  cc:          {', '.join(rendered.cc) or '(none)'}")
    typer.echo(f"  bcc:         {', '.join(rendered.bcc) or '(none)'}")
    typer.echo(f"  recipients:  {rendered.recipient_count} (what the daily cap counts)")
    typer.echo(f"  template:    {rendered.template_hash}")
    if rendered.attachments:
        typer.echo("  attachments:")
        for a in rendered.attachments:
            typer.echo(f"    {a.path}  {a.size/1_000_000:.2f} MB")
        typer.echo(f"    total on the wire: "
                   f"{wire_size(rendered.attachment_bytes)/1_000_000:.2f} MB")
    else:
        typer.echo("  attachments: (none)")

    result = provider.send(rendered)

    conn.execute(
        "INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash, to_addr,"
        " ok, headers, error, sent_at, allowlisted, forced) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (mailbox_id, step_id, campaign or cfg.campaign.name, rendered.template_hash, to,
         int(result.ok), result.headers, result.error, utcnow(), int(not forced), int(forced)),
    )

    if not result.ok:
        typer.secho(f"  FAILED: {result.error}", fg=typer.colors.RED)
        return False

    typer.secho(f"  sent: message {result.message_id} thread {result.thread_id}",
                fg=typer.colors.GREEN)

    typer.echo("\n  --- outgoing headers ---")
    for line in (result.headers or "").splitlines():
        typer.echo(f"  {line}")

    delivered = None
    if hasattr(provider, "delivered_headers") and wait > 0:
        typer.echo(f"\n  waiting up to {wait}s for the delivered copy "
                   f"(SPF/DKIM/DMARC results only exist on it, not on the sent copy)...")
        try:
            delivered = provider.delivered_headers(
                rendered.subject, timeout_seconds=wait, message_id=result.message_id
            )
        except Exception as exc:
            # The send already succeeded. Failing to read the delivered copy is
            # a lost convenience, not a failed send, and must not report as one.
            typer.secho(f"  could not read the delivered copy: {exc}", fg=typer.colors.YELLOW)

    if delivered:
        typer.echo("\n  --- delivered headers ---")
        for line in delivered.splitlines():
            typer.echo(f"  {line}")
        conn.execute(
            "UPDATE test_sends SET headers = ? WHERE id = (SELECT MAX(id) FROM test_sends)",
            ((result.headers or "") + "\n\n--- delivered ---\n" + delivered,),
        )
        if not any(l.lower().startswith("authentication-results") for l in delivered.splitlines()):
            typer.secho(
                "\n  NO AUTHENTICATION RESULTS ON THIS MESSAGE.\n"
                "  A message sent from an account to itself through the same provider never\n"
                "  crosses an authentication boundary, so nothing evaluates SPF, DKIM or\n"
                "  DMARC. Alignment cannot be measured this way.\n"
                "  Send to a receiver on another provider instead:\n"
                "    --to <address>@outlook.com          placement plus real verdicts\n"
                "    --to <one-time address from mail-tester.com>   full report incl. DMARC",
                fg=typer.colors.YELLOW,
            )
        else:
            _summarize_auth(delivered)
    else:
        typer.secho(
            "\n  no delivered copy found in this mailbox. Expected when the recipient is a "
            "different account -- which is also the only way to get real SPF/DKIM/DMARC "
            "verdicts. Open the message at the receiving end and use 'Show original'.",
            fg=typer.colors.YELLOW,
        )
    return True


def _authorize_test_recipient(cfg: Config, conn, mailbox_id: str, step_id: str,
                              campaign: Optional[str], to: str,
                              force: bool) -> tuple[bool, bool]:
    """Gate the one path that can reach an address the review gate never saw.

    Returns (allowed, forced). Suppression is checked regardless of --force and
    is never overridable: an opt-out is permanent and global, and "it was only a
    test" is not an exception a recipient agreed to.
    """
    def refuse(reason: str) -> tuple[bool, bool]:
        typer.secho(f"\n=== test send: mailbox {mailbox_id} ===", fg=typer.colors.CYAN)
        typer.secho(f"  REFUSED: {reason}", fg=typer.colors.RED)
        conn.execute(
            "INSERT INTO test_sends (mailbox_id, step_id, campaign, template_hash, to_addr,"
            " ok, error, sent_at, allowlisted, forced) VALUES (?,?,?,?,?,0,?,?,0,?)",
            (mailbox_id, step_id, campaign or cfg.campaign.name,
             templates.template_hash(cfg), to, reason, utcnow(), int(force)),
        )
        return False, False

    if reason := suppression.is_suppressed(conn, to):
        return refuse(
            f"{to} is on the suppression list ({reason}). Suppression is permanent and "
            f"global, and --force does not override it."
        )

    if cfg.campaign.allows_test_recipient(to):
        return True, False

    if not force:
        return refuse(
            f"{to} is not in campaign.yaml test_send_allowlist and is not test_recipient.\n"
            f"          Allowed: {', '.join(dict.fromkeys([cfg.campaign.test_recipient] + cfg.campaign.test_send_allowlist))}\n"
            f"          Add it there, or pass --force to send to it once."
        )

    typer.secho(
        f"\n  !! FORCED TEST SEND TO AN UNLISTED ADDRESS !!\n"
        f"  {to} is not in test_send_allowlist. This address has not been through the\n"
        f"  review gate and is not a contact in the database. Sending anyway because\n"
        f"  --force was passed. Recorded in test_sends as forced.",
        fg=typer.colors.RED,
    )
    return True, True


def _summarize_auth(headers: str) -> None:
    """Read SPF/DKIM/DMARC verdicts off the delivered copy."""
    line = next(
        (l for l in headers.splitlines() if l.lower().startswith("authentication-results")),
        "",
    ).lower()
    if not line:
        return
    typer.echo("")
    for mech in ("spf", "dkim", "dmarc"):
        if f"{mech}=pass" in line:
            typer.secho(f"  {mech.upper():<6} pass", fg=typer.colors.GREEN)
        elif f"{mech}=fail" in line:
            typer.secho(f"  {mech.upper():<6} FAIL", fg=typer.colors.RED)
        elif f"{mech}=" in line:
            verdict = line.split(f"{mech}=", 1)[1].split()[0]
            typer.secho(f"  {mech.upper():<6} {verdict}", fg=typer.colors.YELLOW)
        else:
            typer.secho(f"  {mech.upper():<6} not reported", fg=typer.colors.YELLOW)
    typer.echo("  (alignment is judged against the From: domain, not Reply-To)")


@app.command("check-links")
def check_links_cmd(
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Verify every linked document resolves without authentication."""
    from . import check_links as CL

    cfg = _config(config)
    conn = open_db(db)
    with transaction(conn):
        results = CL.check_all(conn, cfg, campaign)
    bad = 0
    for name, url, status, detail in results:
        mark = "ok  " if status == "ok" else "FAIL"
        if status != "ok":
            bad += 1
        typer.secho(f"  {mark} {name[:34]:<36} {detail[:52]}",
                    fg=None if status == "ok" else typer.colors.RED)
    typer.echo(f"\n{len(results) - bad}/{len(results)} links publicly reachable")
    if bad:
        raise typer.Exit(1)


# ------------------------------------------------------------------ verify


@app.command("verify")
def verify_cmd(
    limit: int = typer.Option(50, "--limit"),
    recheck: bool = typer.Option(False, "--recheck"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """MX lookup then an SMTP probe. Nothing unverified enters the send queue."""
    from . import verify as V

    cfg = _config(config)
    conn = open_db(db)
    with transaction(conn):
        counts = V.verify_contacts(conn, limit=limit,
                                   mail_from=cfg.campaign.verification.smtp_probe_from,
                                   only_unverified=not recheck)
    total = sum(counts.values()) or 1
    for k in ("valid", "catch_all", "mx_only", "unknown", "invalid"):
        if counts.get(k):
            typer.echo(f"  {k:<10} {counts[k]:>3}  ({100*counts[k]/total:.0f}%)")
    if counts.get("mx_only"):
        typer.secho("\n  mx_only: outbound port 25 is blocked from this network, so the SMTP "
                    "probe cannot run at all. The domain accepts mail; the mailbox is "
                    "unconfirmed. Sendable behind the review gate, and flagged there.",
                    fg=typer.colors.YELLOW)
    if counts.get("catch_all"):
        typer.echo("\n  catch_all sends normally: most Workspace and M365 domains accept "
                   "every recipient, so it is the expected outcome and not a failure.")


# ------------------------------------------------------------------ send


@app.command("send")
def send_cmd(
    limit: int = typer.Option(20, "--limit"),
    campaign: str = typer.Option("startup", "--campaign"),
    mailbox: Optional[str] = typer.Option(None, "--mailbox"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    ignore_window: bool = typer.Option(False, "--ignore-window"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Send approved, verified contacts. One mailbox, paced, by hand."""
    from .send_queue import main as send_main

    argv = ["--limit", str(limit), "--campaign", campaign]
    for flag, value in (("--mailbox", mailbox), ("--config", config), ("--db", db)):
        if value:
            argv += [flag, value]
    if dry_run:
        argv.append("--dry-run")
    if ignore_window:
        argv.append("--ignore-window")
    raise typer.Exit(send_main(argv))


# ------------------------------------------------------------------ demo


@app.command("demo")
def demo(
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    fixtures: Optional[str] = typer.Option(None, "--fixtures"),
):
    """End-to-end on fixture contacts through the console mailbox. No network.

    Writes to a scratch database by default. It used to default to the real one,
    which quietly seeded three fake companies into production data.
    """
    cfg = _config(config)
    conn = open_db(db or ROOT / "state" / "demo.db")
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
        " to_addr, cc, bcc, recipient_count, subject, body_hash, template_hash,"
        " attachment_names, idempotency_key, queued_at, sending_at)"
        " VALUES (?,?,?,?,'sending',?,?,?,?,?,?,?,?,?,?,?)",
        (contact_id, campaign_id, step_id, mailbox_id, rendered.to,
         ",".join(rendered.cc), ",".join(rendered.bcc), rendered.recipient_count,
         rendered.subject, rendered.body_hash, rendered.template_hash,
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
