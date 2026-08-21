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
from .normalize import display_company, normalize_company, registrable_domain
from . import prefilter as prefilter_mod
from . import providers, suppression, templates

app = typer.Typer(add_completion=False, help="Outbound sourcing: discovery, review, send.")
db_app = typer.Typer(help="Database maintenance.")
suppress_app = None   # suppress is a plain command, not a group
app.add_typer(db_app, name="db")

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
    file: str = typer.Option(..., "--file", help="one company per line: name or name,domain"),
    tier: Optional[str] = typer.Option(None, "--tier"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Load a list of companies into the accounts table.

    The way in when you already have targets. Finding people inside them is
    `outbound investigate`.
    """
    argv = ["--mode", "list", "--file", file]
    for flag, value in (("--tier", tier),
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
        # Without this the preview and the test send both rendered base
        # templates and recorded the base hash whatever --campaign said, so the
        # gate compared against templates the campaign would never use.
        campaign=campaign,
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


@app.command("exclusions")
def exclusions_cmd(
    refresh: bool = typer.Option(True, "--refresh/--no-refresh"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Show who is excluded as personally known, and the evidence for each hop."""
    from . import exclusions as EX

    cfg = _config(config)
    conn = open_db(db)
    if refresh:
        with transaction(conn):
            EX.refresh(conn, cfg)
    rows = list(conn.execute(
        "SELECT * FROM exclusions_applied ORDER BY hops, through, name"))
    if not rows:
        typer.echo("nothing excluded. Named seeds match no node in the graph yet.")
        typer.echo("Seeds are matched against graph_nodes, so they take effect once "
                   "traversal has seen them.")
        return
    seeds = [r for r in rows if r["hops"] == 0]
    hop1 = [r for r in rows if r["hops"] == 1]
    typer.secho(f"\n{len(seeds)} named directly:", fg=typer.colors.CYAN)
    for r in seeds:
        typer.echo(f"  {r['name']}\n      {r['reason'][:110]}")
    typer.secho(f"\n{len(hop1)} excluded one hop out:", fg=typer.colors.CYAN)
    for r in hop1:
        typer.echo(f"  {r['name']}\n      {r['reason']}\n      {r['source_url'] or '(no url)'}")
    typer.echo(f"\ntotal excluded: {len(rows)}")


# ------------------------------------------------------- /outbound support
#
# These exist so the slash command's agentic half never has to guess at
# mechanism. Each one is deterministic, prints what it did, and meters itself.


run_app = typer.Typer(add_completion=False, help="Per-run cost accounting.")
app.add_typer(run_app, name="run")


@run_app.command("start")
def run_start(target: str = typer.Argument(...),
              kind: str = typer.Option("company", "--kind"),
              scratch: bool = typer.Option(False, "--scratch",
                                           help="print an OUTBOUND_DB for a test run")):
    """Open a run file. Export OUTBOUND_RUN_ID so later steps accumulate into it."""
    from . import meter
    import re as _re, time as _time

    slug = _re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")
    run_id = f"{slug}-{int(_time.time())}"
    meter.start(run_id, target, kind)
    if scratch:
        scratch_db = meter.runs_dir() / f"{run_id}.db"
        typer.echo(f"{run_id}\nOUTBOUND_DB={scratch_db}", err=False)
        return
    typer.echo(run_id)


@run_app.command("log")
def run_log(run_id: Optional[str] = typer.Option(None, "--run"),
            searches: int = typer.Option(0, "--searches"),
            fetches: int = typer.Option(0, "--fetches"),
            label: str = typer.Option("agent", "--label")):
    """Record agent-side WebSearch/WebFetch counts, which nothing else can see."""
    from . import meter
    import os

    rid = run_id or os.environ.get("OUTBOUND_RUN_ID")
    if not rid:
        raise typer.BadParameter("no run id; pass --run or set OUTBOUND_RUN_ID")
    if searches:
        meter.bump("agent_searches", searches)
    if fetches:
        meter.bump("agent_fetches", fetches)
    meter.flush(rid, label)
    typer.echo(f"logged to {rid}")


@run_app.command("report")
def run_report(run_id: Optional[str] = typer.Option(None, "--run")):
    """What this run cost: calls, fetches, wall time."""
    from . import meter
    import os

    rid = run_id or os.environ.get("OUTBOUND_RUN_ID")
    data = meter.report(rid)
    t = data["totals"]
    typer.secho(f"\n=== cost: {data.get('target', rid)} ===", fg=typer.colors.CYAN)
    for k in ("agent_searches", "agent_fetches", "http_fetches", "openalex_calls",
              "openalex_throttled", "smtp_probes"):
        if t.get(k):
            typer.echo(f"  {k:<20} {int(t[k])}")
    typer.echo(f"  {'wall clock':<20} {data['wall_s']}s"
               f"   ({round(data['wall_s'] / 60, 1)} min)")
    typer.echo(f"  {'steps':<20} {len(data['steps'])}")


@app.command("company-resolve")
def company_resolve(
    name: str = typer.Argument(...),
    domain: Optional[str] = typer.Option(None, "--domain"),
    tier: Optional[str] = typer.Option(None, "--tier"),
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    ai_depth: Optional[str] = typer.Option(None, "--ai-depth", help="builds | applies"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Resolve one company: domain, homepage liveness, suppression, exclusions.

    Step 1 of a run. Fails loudly rather than quietly proceeding on a company
    that is suppressed or that the operator already knows.
    """
    from . import exclusions as EX
    from . import meter
    from .homepages import fetch_one

    cfg = _config(config)
    conn = open_db(db)
    typer.secho(f"\n=== resolve: {name} ===", fg=typer.colors.CYAN)

    if reason := suppression.is_suppressed(conn, f"x@{domain or 'unknown.invalid'}", name):
        typer.secho(f"  SUPPRESSED: {reason}", fg=typer.colors.RED)
        typer.secho("  stop here. Nothing from this company may enter the queue.",
                    fg=typer.colors.RED)
        raise typer.Exit(2)
    if hit := EX.check(conn, cfg, name, company=name):
        typer.secho(f"  PERSONALLY EXCLUDED: {hit['reason']}", fg=typer.colors.RED)
        raise typer.Exit(2)
    typer.echo("  suppression: clear")
    typer.echo("  personal exclusions: clear")

    from . import claims as C
    if cfg.campaign.claims_file:
        others = C.held_by_others(cfg.campaign.claims_file, C.COMPANY, name)
        line = C.warn_line(others, C.COMPANY, name,
                           cfg.campaign.claims_stale_after_days)
        if line:
            typer.secho(f"  CLAIMED: {line}", fg=typer.colors.YELLOW)
            typer.echo("           two emails from one group to one company in a week "
                       "reads as disorganised.")
        else:
            typer.echo("  claims: nobody else is working on this")

    # normalize_company is the key ingest matches on. Using anything else here
    # creates a second account row for the same company, and the routing written
    # to the first never reaches the contacts attached to the second -- which is
    # exactly what happened: LOWER(name) matched nothing ingest would look up.
    key = normalize_company(name)
    row = conn.execute("SELECT id, name, domain, liveness_status, liveness_note, status"
                       " FROM accounts WHERE name_normalized = ?", (key,)).fetchone()
    if row:
        typer.echo(f"  known account id={row['id']} domain={row['domain'] or '(none)'} "
                   f"status={row['status']} liveness={row['liveness_status'] or '(unchecked)'}")
        domain = domain or row["domain"]
    else:
        typer.echo("  not in accounts yet (new company)")

    if domain:
        r = fetch_one(f"https://{domain}/")
        typer.echo(f"  homepage https://{domain}/ -> {r.status} ({r.detail})")
        if r.status == "ok":
            typer.echo(f"    {' '.join(r.text.split())[:160]}")
    else:
        typer.secho("  no domain known. Find it before resolving emails.",
                    fg=typer.colors.YELLOW)

    # Routing. A company discovered fresh has never been through the classifier,
    # so without this its contacts land with campaign NULL: invisible to a
    # campaign-scoped review export and unable to ever send. They look ingested
    # and are unreachable, which is the worst shape of failure -- nothing errors.
    campaign = campaign or (cfg.campaigns.for_tier(tier) if tier else None)
    if campaign:
        cfg.campaigns.get(campaign)          # raises on an unknown name
    if not campaign:
        # Refuse before writing. Creating the row and then refusing leaves an
        # unroutable account behind, which is the state this check exists to
        # prevent -- and the next run would find it and treat it as known.
        meter.flush(label="company-resolve")
        typer.secho(
            f"\n  REFUSING to register {name!r} with no campaign. Its contacts would "
            f"ingest cleanly and then be unroutable -- absent from every campaign review "
            f"export and unable to send, with nothing reporting an error.\n"
            f"  Re-run with --tier <tier> or --campaign <name>. Depth routes: "
            f"{cfg.campaigns.depth_routes() or '(see campaigns.yaml)'}",
            fg=typer.colors.RED)
        raise typer.Exit(3)
    with transaction(conn):
        if row:
            conn.execute("UPDATE accounts SET domain=COALESCE(?, domain),"
                         " tier=COALESCE(?, tier), campaign=COALESCE(?, campaign),"
                         " ai_depth=COALESCE(?, ai_depth), updated_at=? WHERE id=?",
                         (domain, tier, campaign, ai_depth, utcnow(), row["id"]))
            aid = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO accounts (name, name_normalized, domain, source, status,"
                " tier, campaign, ai_depth, created_at, updated_at)"
                " VALUES (?,?,?,?,'new',?,?,?,?,?)",
                (name, key, domain, "outbound-run", tier, campaign, ai_depth,
                 utcnow(), utcnow()))
            aid = cur.lastrowid
    meter.flush(label="company-resolve")
    typer.echo(f"  routed: account {aid} -> tier={tier} campaign={campaign} "
               f"ai_depth={ai_depth or '(unset)'}")


@app.command("person-pages")
def person_pages_cmd(
    names: list[str] = typer.Argument(...),
    url: Optional[str] = typer.Option(None, "--url", help="try this URL first"),
    company: str = typer.Option(..., "--company",
                                help="the page must mention this; required"),
    json_out: Optional[str] = typer.Option(None, "--json"),
):
    """Probe personal pages for addresses. Observed only -- never a guess.

    --company is required, not optional. Without it a guessed URL that lands on
    a namesake looks like a clean hit: probing "Pankaj Gupta" for Baseten found
    a real page belonging to a different Pankaj Gupta and read a stranger's
    address off it, with every other check green. Making the corroboration
    opt-in would mean remembering to opt in, and this failed silently once.
    """
    from . import meter
    from .person_pages import find
    import concurrent.futures

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda n: find(n, [url] if url else None, company), names):
            results.append(r)
            mark = {"found": "FOUND", "namesake_risk": "?????", "no_email": "page ",
                    "not_found": "  -  "}.get(r.status, r.status)
            colour = {"found": typer.colors.GREEN,
                      "namesake_risk": typer.colors.YELLOW}.get(r.status)
            typer.secho(f"  {mark} {r.name[:26]:<28} {(r.url or '')[:44]:<46}"
                        f"{', '.join(r.emails[:2])}", fg=colour)
    found = sum(1 for r in results if r.status == "found")
    risky = sum(1 for r in results if r.status == "namesake_risk")
    pages = sum(1 for r in results if r.status == "no_email")
    typer.echo(f"\n  {len(results)} probed: {found} with an address, "
               f"{pages} page but no address, "
               f"{len(results) - found - pages - risky} no page found")
    # How much search recovered over guessing. Reported per run rather than
    # asserted once, because the answer depends on how conventional the
    # population's handles are and that is not stable across companies.
    by_source: dict[str, int] = {}
    for r in results:
        if r.status in ("found", "namesake_risk", "no_email") and r.source:
            by_source[r.source] = by_source.get(r.source, 0) + 1
    if by_source:
        typer.echo("  pages reached by source: " + ", ".join(
            f"{n} {k}" for k, n in sorted(by_source.items(), key=lambda kv: -kv[1])))
        recovered = sum(n for k, n in by_source.items() if k != "guess")
        typer.echo(f"    {recovered} of {sum(by_source.values())} would have been missed "
                   f"by URL guessing alone")
    if risky:
        typer.secho(f"  {risky} address(es) found on a page that never mentions "
                    f"{company!r}. Treated as a namesake, not a contact. Confirm by hand "
                    f"or discard.", fg=typer.colors.YELLOW)
    if json_out:
        Path(json_out).write_text(json.dumps(
            [{"name": r.name, "status": r.status, "url": r.url, "emails": r.emails,
              "corroborated": r.corroborated, "tried": r.tried}
             for r in results], indent=2))
        typer.echo(f"  wrote {json_out}")
    meter.flush(label="person-pages")


@app.command("candidates-from-pages")
def candidates_from_pages(
    pages_json: str = typer.Option(..., "--json", help="output of person-pages --json"),
    company: str = typer.Option(..., "--company"),
    domain: str = typer.Option(..., "--domain"),
    out: str = typer.Option(..., "--out"),
    prefer_domain: bool = typer.Option(True, "--prefer-domain/--first-address"),
):
    """Turn probe results into a candidate file with the address evidence filled in.

    The evidence contract requires an entry that grounds the address itself, and
    the probe already knows which page each address was read off. Writing that
    by hand is a step that can be forgotten -- and was, on the first attempt, by
    the agent that had just run the probe. Generated here instead.

    Title and personalization are deliberately left blank: those are judgment,
    and a machine-filled placeholder would pass the validator while saying
    nothing. Fill them in before ingesting.
    """
    data = json.loads(Path(pages_json).read_text())
    out_rows = []
    for r in data:
        if r["status"] != "found" or not r["emails"]:
            continue
        emails = r["emails"]
        if prefer_domain:
            emails = sorted(emails, key=lambda e: (domain not in e.partition("@")[2],))
        email = emails[0]
        out_rows.append({
            "name": r["name"],
            "title": "TODO",
            "company": company,
            "email": email,
            "email_basis": "observed",
            "confidence": 0.9 if domain in email else 0.65,
            "country": "US",
            "personalization": None,
            "evidence": [{
                "claim": f"{r['name']}'s address {email} is published on their own page",
                "url": r["url"],
                "quote": email,
                "retrieved_at": utcnow(),
            }],
            "_other_addresses": emails[1:] or None,
        })
    Path(out).write_text(json.dumps(
        {"company": company, "domain": domain, "generated_at": utcnow(),
         "candidates": out_rows}, indent=2))
    typer.echo(f"  wrote {len(out_rows)} candidate(s) to {out}")
    typer.secho("  title and personalization are TODO/null by design -- fill them in, "
                "then ingest.", fg=typer.colors.YELLOW)


def _account_name_for(conn, contact_id: int) -> str:
    row = conn.execute("SELECT a.name FROM contacts c JOIN accounts a"
                       " ON a.id = c.account_id WHERE c.id = ?", (contact_id,)).fetchone()
    return row["name"] if row else ""


@app.command("claim")
def claim_cmd(
    value: str = typer.Argument(..., help="a company name, or a person's email"),
    note: str = typer.Option("", "--note"),
    config: Optional[str] = typer.Option(None, "--config"),
):
    """Say you are working on a company, so nobody else in the group starts too.

    Optional. Sending records a claim by itself -- this is for staking one out
    before you have written anything.
    """
    from . import claims as C

    cfg = _config(config)
    if not cfg.campaign.claims_file:
        typer.secho("  no claims file configured. Set campaign.claims_file to a path "
                    "everyone in the group syncs (a git repo, Drive, Dropbox).",
                    fg=typer.colors.YELLOW)
        raise typer.Exit(2)
    kind = C.PERSON if "@" in value else C.COMPANY
    others = C.held_by_others(cfg.campaign.claims_file, kind, value)
    if others:
        typer.secho("  " + C.warn_line(others, kind, value,
                                       cfg.campaign.claims_stale_after_days),
                    fg=typer.colors.YELLOW)
    claim = C.add(cfg.campaign.claims_file, kind, value, note=note)
    typer.secho(f"  claimed {kind} {claim.value!r} as {claim.who}",
                fg=typer.colors.GREEN)
    typer.echo(f"  appended to {cfg.campaign.claims_file} -- commit and push it, "
               f"or let your shared folder sync")


@app.command("claims")
def claims_cmd(
    stale: bool = typer.Option(False, "--stale", help="only stale claims"),
    config: Optional[str] = typer.Option(None, "--config"),
):
    """Who is working on what."""
    from . import claims as C

    cfg = _config(config)
    rows = C.load(cfg.campaign.claims_file)
    if not rows:
        typer.echo("no claims. Either nobody has claimed anything, or "
                   "campaign.claims_file is not set.")
        return
    days = cfg.campaign.claims_stale_after_days
    rows = [r for r in rows if not stale or r.is_stale(days)]
    for r in sorted(rows, key=lambda r: r.claimed_at, reverse=True):
        mark = "stale" if r.is_stale(days) else "    "
        typer.echo(f"  {mark}  {r.kind:<8} {r.value[:34]:<36} {r.describe(days)}")
    typer.echo(f"\n  {len(rows)} claim(s). Stale after {days} days.")


@app.command("merge-accounts")
def merge_accounts_cmd(
    acquired: str = typer.Argument(..., help="the company that was acquired"),
    acquirer: str = typer.Argument(..., help="the company that bought it"),
    source_url: str = typer.Option(..., "--source", help="the announcement"),
    reason: str = typer.Option("acquired", "--reason"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Fold an acquired company into its acquirer.

    A target list is a snapshot, and any list long enough contains both sides of
    a deal. Queueing both means two emails into one company, one addressed to a
    business unit that no longer trades under that name. The acquired row is
    kept rather than deleted: its people, evidence and history stay attached and
    the merge records why.
    """
    from . import merge_accounts as MA

    conn = open_db(db)
    with transaction(conn):
        result = MA.merge(conn, acquired=acquired, acquirer=acquirer,
                          reason=reason, source_url=source_url)
    typer.secho(f"  merged {acquired!r} into {acquirer!r}", fg=typer.colors.GREEN)
    for k, v in result.items():
        typer.echo(f"    {k.replace('_', ' ')}: {v}")
    typer.echo(f"  accounts still queueable: {MA.queueable(conn)}")


@app.command("suggest")
def suggest_cmd(
    terms: str = typer.Argument(..., help="industry terms, comma-separated"),
    pick: Optional[str] = typer.Option(None, "--pick",
                                       help="which to take, e.g. 1,3,5 or 1-4"),
    limit: int = typer.Option(40, "--limit"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Companies already on hand whose own words match an industry.

    Industry mode's first half. Deciding what belongs to "AI inference" is
    judgment and stays with the operator; this lays out the candidates already
    in the database so the decision is made against real descriptions rather
    than from memory. Descriptions come from each company's own words, never an
    investor blurb.
    """
    from . import suggest as S

    conn = open_db(db)
    rows = S.from_accounts(conn, [t.strip() for t in terms.split(",") if t.strip()],
                           limit=limit)
    if not rows:
        typer.echo("nothing on hand matches. Import a list first: outbound discover "
                   "--file companies.txt")
        return
    for i, sug in enumerate(rows, 1):
        typer.echo(f"  [{i:>2}] {sug.name[:30]:<32} {(sug.domain or '')[:26]:<28}"
                   f"{(sug.description or '')[:60]}")
    if not pick:
        typer.echo(f"\n  {len(rows)} candidate(s). Choose with --pick 1,3,5 (or 1-4).")
        return
    chosen = S.parse_selection(pick, len(rows))
    typer.secho(f"\n  {len(chosen)} chosen. Run these:", fg=typer.colors.CYAN)
    for i in chosen:
        sug = rows[i - 1]
        typer.echo(f"    outbound investigate \"{sug.name}\" "
                   f"--domain {sug.domain or '<domain>'}")


@app.command("ask")
def ask_cmd(
    kind: str = typer.Argument(..., help="company | industry | senior | ambiguous"),
    subject: str = typer.Option(..., "--subject", help="company, field or person"),
    context: str = typer.Option("", "--context", help="where it came up"),
    found: int = typer.Option(0, "--found", help="how many people"),
    title: str = typer.Option("", "--title"),
    detail: str = typer.Option("", "--detail", help="for `ambiguous`"),
):
    """Render one of the four questions the run is allowed to stop for.

    Rendered here rather than formatted from memory each time. The design is the
    point -- one line of context, one question, two or three one-word answers --
    and a question hand-written twelve different ways stops being answerable at a
    glance, which was the whole reason for the shape.
    """
    from . import ask as A

    q = {
        "company": lambda: A.company_found(found, subject, context),
        "industry": lambda: A.industry_adjacent(subject, context),
        "senior": lambda: A.senior_person(subject, title, context),
        "ambiguous": lambda: A.ambiguous_claim(subject, detail),
    }.get(kind)
    if not q:
        raise typer.BadParameter("kind must be company, industry, senior or ambiguous")
    typer.echo(q().render())


@app.command("skip")
def skip_cmd(
    kind: str = typer.Argument(..., help=" | ".join(("leadership", "namesake",
                                                    "no-address", "inferred",
                                                    "wrong-employer", "answered",
                                                    "suppressed"))),
    subject: str = typer.Option(..., "--subject", help="the person"),
    context: str = typer.Option("", "--context",
                                help="the page, company, or answer given"),
):
    """Render one skip line, saying whether it is recoverable or final.

    Same reason as `ask`: the phrasing carries the meaning. Recoverable and final
    need different reactions from the operator -- one is a name to chase by hand,
    the other a name to forget -- and that distinction survives only if it is
    written the same way every time.
    """
    from . import ask as A

    build = A.SKIP_BUILDERS.get(kind)
    if not build:
        raise typer.BadParameter(f"kind must be one of {', '.join(A.SKIP_BUILDERS)}")
    skip = build(subject, context) if context else build(subject, "")
    typer.echo(f"{skip.name}: {skip.line()}")


@app.command("doctor")
def doctor_cmd(
    config: Optional[str] = typer.Option(None, "--config"),
):
    """What is wrong, and the exact command or click that fixes it.

    Run this when something does not work. Exits non-zero if anything failed, so
    install.sh and CI can gate on it.
    """
    from . import doctor as D

    root = Path(config) if config else ROOT / "config"
    typer.secho(f"\noutbound doctor -- {root}\n", fg=typer.colors.CYAN)
    checks = D.run(root)
    colours = {D.OK: typer.colors.GREEN, D.WARN: typer.colors.YELLOW,
               D.FAIL: typer.colors.RED}
    marks = {D.OK: "ok  ", D.WARN: "warn", D.FAIL: "FAIL"}
    for c in checks:
        typer.secho(f"  {marks[c.status]}  {c.name:<34} {c.detail[:70]}",
                    fg=colours[c.status])
    bad = [c for c in checks if c.status != D.OK]
    if not bad:
        typer.secho("\n  everything checks out.", fg=typer.colors.GREEN)
        return
    typer.secho(f"\n  {len(bad)} thing(s) to fix:", fg=typer.colors.CYAN)
    for c in bad:
        typer.secho(f"\n  {c.name}", fg=colours[c.status], bold=True)
        typer.echo(f"      {c.detail}")
        for line in c.fix:
            typer.echo(f"      $ {line}" if not line.startswith(("or ", "then ",
                                                                "  ", "a ", "the ",
                                                                "this ", "public ",
                                                                "verify ", "check ",
                                                                "base64 ", "use ",
                                                                "app ", "create ",
                                                                "enable ", "paste ",
                                                                "wait ", "compress ",
                                                                "set ", "fix "))
                        else f"        {line}")
    raise typer.Exit(1)


@app.command("investigate")
def investigate_cmd(
    company: str = typer.Argument(...),
    domain: str = typer.Option(..., "--domain"),
    seed: Optional[str] = typer.Option(None, "--seed", help="names, comma-separated"),
    budget: int = typer.Option(60, "--budget", help="max steps"),
    max_dry: int = typer.Option(8, "--max-dry"),
    run_id: str = typer.Option("inv", "--run-id"),
    json_out: Optional[str] = typer.Option(None, "--json"),
):
    """Chase evidence adaptively until a contact is grounded or the budget runs out.

    Not a channel sequence. Each step asks what investigation gets closest to a
    grounded contact and takes it: a page with no address but a Scholar link is
    a lead, not a dead end; a paper carrying company addresses makes every
    coauthor a new lead. Stops on budget or on max-dry consecutive steps that
    yield neither a fact nor a lead.
    """
    from . import investigate as I
    from . import meter

    seeds = [I.Lead("person", n.strip(), n.strip(), "operator seed")
             for n in (seed or "").split(",") if n.strip()]
    # Without a seed, start from the domain's own convention: it turns every
    # name found later into an address, and it names people directly.
    seeds.append(I.Lead("domain_pattern", domain, "", "no seed given; start from "
                        "the domain's email convention"))
    inv = I.run(company, domain, seeds, budget=budget, max_dry=max_dry)

    typer.secho(f"\n=== {company}: {len(inv.steps)} steps ===", fg=typer.colors.CYAN)
    for st in inv.steps:
        mark = "+" if st.productive else " "
        typer.echo(f"  {mark} {st.lead.kind:<15} {st.lead.value[:36]:<38} {st.outcome[:40]}")
    people = inv.people()
    done = [n for n in people if inv.complete(n)]
    guessed = [n for n in people if inv.inferred_only(n)]
    typer.echo(f"\n  {len(inv.facts)} facts, {len(people)} people touched")
    typer.secho(f"  {len(done)} OBSERVED  -- an address seen on a page, a paper or "
                f"a commit", fg=typer.colors.GREEN)
    typer.secho(f"  {len(guessed)} INFERRED  -- derived from the domain convention; "
                f"never seen for this person",
                fg=typer.colors.YELLOW if guessed else None)
    for n in done:
        got = inv.person_facts(n)
        typer.secho(f"    {n[:24]:<26} {got['email'].value:<34} "
                    f"title={got['title'].value if 'title' in got else 'UNKNOWN'}",
                    fg=typer.colors.GREEN)
    if guessed:
        typer.secho(f"\n  {len(guessed)} address(es) inferred from the domain "
                    f"convention, not observed:", fg=typer.colors.YELLOW)
        for n in guessed[:12]:
            typer.echo(f"    {n[:24]:<26} {inv.person_facts(n)['email_inferred'].value}")
        typer.echo("    These are marked inferred_from_pattern at ingest and flagged "
                   "at review.")
    typer.echo(f"  stopped: {inv.stopped_because}")

    from . import ask as A
    read_first = []
    if guessed:
        read_first.append(f"{len(guessed)} address(es) inferred from the domain "
                          f"convention, never observed")
    typer.secho("\n" + A.run_summary(
        drafted=[(n, company) for n in done],
        skipped=[(n, "address inferred, not observed") for n in guessed],
        read_first=read_first), fg=None)
    log = inv.write_log(run_id)
    typer.echo(f"  log: {log}")
    if json_out:
        Path(json_out).write_text(json.dumps(
            {n: {k: {"value": f.value, "url": f.url, "quote": f.quote}
                 for k, f in inv.person_facts(n).items()} for n in people}, indent=2))
        typer.echo(f"  wrote {json_out}")
    meter.flush(label="investigate")


@app.command("hf-org")
def hf_org_cmd(
    company: str = typer.Argument(...),
    verify: Optional[str] = typer.Option(None, "--verify", help="check one name"),
    limit: int = typer.Option(40, "--limit"),
):
    """Current employees from a company's Hugging Face organisation.

    The verification oracle the other channels lack. An OpenAlex affiliation
    records where someone worked when a paper was submitted; cross-checking 15
    Hugging Face addresses against the live org list found 9 had left. It is
    also the only channel that works for companies which do not publish --
    OpenAlex found 1 person at Baseten, whose HF org lists 120.
    """
    from . import hf_org

    if verify:
        ok, why = hf_org.check(company, verify)
        colour = {True: typer.colors.GREEN, False: typer.colors.RED}.get(ok)
        typer.secho(f"  {verify}: {ok}\n    {why}", fg=colour)
        raise typer.Exit(0 if ok else 1)

    slug = hf_org.slug_for(company)
    if not slug:
        typer.secho(f"  no Hugging Face org mapped for {company!r}. Add it to "
                    f"hf_org.ORG_SLUGS.", fg=typer.colors.YELLOW)
        raise typer.Exit(2)
    roster = hf_org.members(slug)
    typer.secho(f"\n  {len(roster)} current member(s) of {slug!r}", fg=typer.colors.CYAN)
    for m in roster[:limit]:
        typer.echo(f"    {m['name'][:32]:<34} @{m['user']}")
    if len(roster) > limit:
        typer.echo(f"    ... and {len(roster) - limit} more")
    typer.echo("\n  Org membership is strong evidence of a current association, not a "
               "contract of employment. Use it to filter, never to assert a title.")


@app.command("paper-emails")
def paper_emails_cmd(
    company: str = typer.Argument(...),
    domain: str = typer.Option(..., "--domain"),
    terms: Optional[str] = typer.Option(None, "--terms", help="extra search phrases, comma-sep"),
    max_papers: int = typer.Option(10, "--max-papers"),
    max_age: int = typer.Option(2, "--max-age-years",
                                help="papers older than this do not prove current employment"),
    json_out: Optional[str] = typer.Option(None, "--json"),
):
    """Read author addresses off paper first pages. For the population personal
    pages miss.

    Together AI's researchers keep personal sites; Groq's do not -- 48 of 72 had
    no page at all. Systems and hardware people publish at ISCA/MICRO/ASPLOS
    instead, and those papers print author emails at the company domain under
    the title. Same evidence contract: the address comes from a document the
    person wrote, and the paper is the citation.
    """
    from . import meter
    from .paper_emails import harvest, pair

    extra = [t.strip() for t in (terms or "").split(",") if t.strip()]
    hits, skipped = harvest(company, domain, extra_terms=extra, max_papers=max_papers,
                            max_age_years=max_age)
    rows, conventions = [], {}
    for h in hits:
        att, counts = pair(h)
        for style, n in counts.items():
            conventions[style] = conventions.get(style, 0) + n
        typer.secho(f"\n  arxiv:{h.arxiv_id}  {h.title[:62]}", fg=typer.colors.CYAN)
        for email, author in sorted(att.items(), key=lambda kv: kv[1]):
            typer.echo(f"    {author[:26]:<28} {email}")
            rows.append({"name": author, "email": email, "company": company,
                         "arxiv_id": h.arxiv_id, "title_of_paper": h.title,
                         "url": f"https://arxiv.org/abs/{h.arxiv_id}"})
        for email in h.emails:
            if email not in att:
                typer.secho(f"    {'(unattributed)':<28} {email}", fg=typer.colors.YELLOW)

    typer.echo(f"\n  {len(rows)} attributed address(es) at {domain} "
               f"from {len(hits)} paper(s)")
    if conventions:
        top = max(conventions.items(), key=lambda kv: kv[1])
        typer.echo(f"  domain convention: {top[0]} ({top[1]} sample(s)) "
                   f"-- applies to colleagues not on these papers")
    if skipped:
        typer.secho(f"  skipped {len(skipped)} paper(s) older than {max_age}y: "
                    f"{', '.join(skipped[:4])}", fg=typer.colors.YELLOW)
        typer.echo("    An address on an old paper proves where someone worked then, "
                   "not now.")
    if not rows:
        typer.secho("  no addresses. Either the papers are paywalled, the venue does "
                    "not print them, or they were all too old.", fg=typer.colors.YELLOW)
    if json_out:
        Path(json_out).write_text(json.dumps(rows, indent=2))
        typer.echo(f"  wrote {json_out}")
    meter.flush(label="paper-emails")


@app.command("traverse-company")
def traverse_company(
    name: str = typer.Argument(...),
    expand: int = typer.Option(0, "--expand", help="expand the top N entry points"),
    since: int = typer.Option(2022, "--since"),
    run: str = typer.Option("manual", "--run-id"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Entry points into a company, from authors who named it as their own affiliation.

    Expansion is off by default. On the three companies measured, a company seed
    yielded its people from the affiliation query and expansion returned the
    surrounding research community rather than more employees -- so paying for
    it has to be a decision, not a default.
    """
    from . import graph as G, meter, traverse
    from .openalex import Client

    conn = open_db(db)
    client = Client(mailto=_config(None).campaign.contact_email or None)
    typer.secho(f"\n=== traverse: {name} ===", fg=typer.colors.CYAN)
    with transaction(conn):
        oid, people = traverse.seed_company(conn, client, name, run)
    typer.echo(f"  entry points (own affiliation string): {len(people)}")
    ranked = sorted(people.values(), key=lambda r: -r["papers"])
    for rec in ranked[:15]:
        ids = len(rec.get("openalex_ids") or [1])
        typer.echo(f"    {rec['papers']:>2}p  {rec['latest']}  {rec['name'][:34]:<36}"
                   f"{f'  ({ids} openalex ids merged)' if ids > 1 else ''}")
    if len(ranked) > 15:
        typer.echo(f"    ... and {len(ranked) - 15} more")

    if expand:
        reached = {}
        for rec in ranked[:expand]:
            row = conn.execute("SELECT id FROM graph_nodes WHERE kind='person'"
                               " AND display_name = ?", (rec["name"],)).fetchone()
            if not row:
                continue
            with transaction(conn):
                e = traverse.expand(conn, client, row["id"], run, seed_node_id=oid,
                                    hops=2, via="works_at", since=since, min_papers=1)
            for info in e.coauthors.values():
                if info["name"].strip().lower() not in people:
                    reached[info["name"]] = info
        typer.echo(f"  expanded {min(expand, len(ranked))} -> reached {len(reached)} "
                   f"new people at 1 hop")
    typer.echo(f"  openalex calls: {client.calls}"
               f"{f', throttled {client.throttled}x' if client.throttled else ''}")
    meter.flush(label="traverse-company")


# ------------------------------------------------------------------ suppress


def _infer_kind(value: str) -> str:
    """An address, a domain, or a company name, told apart by shape."""
    if "@" in value:
        return "email"
    if "." in value and " " not in value:
        return "domain"
    return "company"


@app.command("suppress")
def suppress_add(
    value: str = typer.Argument(..., help="Address, domain, or company."),
    kind: Optional[str] = typer.Option(None, "--kind",
                                       help="email | domain | company | lab"),
    reason: str = typer.Option("asked to stop", "--reason"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Suppress someone the moment they ask. Permanent and global.

    One line with nothing to remember, because honoring an opt-out is the
    obligation and any friction here is friction on the thing that matters.
    Kind is inferred from shape unless you say otherwise.
    """
    kind = kind or _infer_kind(value)
    cfg = _config(config)
    conn = open_db(db)
    with transaction(conn):
        added = suppression.add(conn, kind, value, reason,
                                csv_path=cfg.root / "suppression.csv")
        if kind == "company":
            suppression.suppress_company(conn, value, reason,
                                         csv_path=cfg.root / "suppression.csv")
        elif kind == "lab":
            # A reply from one member of a research group stops the whole group.
            # Company-level suppression misses this: four people from one
            # university lab are colleagues who talk to each other, and the loop
            # surfaces them together.
            suppression.suppress_lab(conn, value, reason,
                                     csv_path=cfg.root / "suppression.csv")
    typer.secho(f"{'suppressed' if added else 'already suppressed'}: {kind} {value}",
                fg=typer.colors.GREEN)


@app.command("suppressions")
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

    Real template, real persona, real attachments, real CC list,
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
        # Gmail silently rewrites From to the authenticated account unless the
        # address is a verified "Send mail as" alias. The sent copy shows what we
        # asked for and the delivered copy shows what the recipient sees, so the
        # mismatch is only visible here -- and it changes who the recipient
        # thinks is writing, which is the whole point of the From line.
        from email.utils import parseaddr as _pa

        want = _pa(rendered.from_header)[1].lower()
        got = ""
        for line in delivered.splitlines():
            if line.lower().startswith("from:"):
                got = _pa(line.partition(":")[2])[1].lower()
                break
        if want and got and want != got:
            typer.secho(
                f"\n  FROM WAS REWRITTEN IN TRANSIT.\n"
                f"  configured: {want}\n"
                f"  delivered as: {got}\n"
                f"  Gmail only honours a From address that is a verified "
                f"'Send mail as' alias on the sending account. Until {want} is "
                f"verified, every recipient sees {got} no matter what the config "
                f"says.\n"
                f"  Fix in Gmail: Settings -> Accounts and Import -> 'Send mail as' "
                f"-> Add another email address -> {want}, then enter the code Gmail "
                f"emails to that address. Re-run this test afterwards.",
                fg=typer.colors.RED)
        elif want and got:
            typer.secho(f"\n  From verified end to end: delivered as {got}",
                        fg=typer.colors.GREEN)

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


review_app = typer.Typer(add_completion=False, help="The human review gate.")
app.add_typer(review_app, name="review")


@review_app.command("export")
def review_export(
    out: str = typer.Option("review.md", "--out"),
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Write the review packet: a markdown brief plus a CSV to mark up."""
    from . import review as R
    from pathlib import Path as _P

    conn = open_db(db)
    n, flagged = R.export(conn, _config(config), _P(out), campaign)
    csv_path = _P(out).with_suffix(".csv")
    typer.echo(f"exported {n} contact(s), {flagged} flagged")
    typer.echo(f"  brief:  {out}")
    typer.echo(f"  decide: {csv_path}   (set approved to y or n, then: "
               f"outbound review import --file {csv_path})")


@review_app.command("import")
def review_import(
    file: str = typer.Option(..., "--file"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Load review decisions back in. Nothing sends until this has run."""
    from . import review as R
    from pathlib import Path as _P

    conn = open_db(db)
    with transaction(conn):
        counts = R.import_decisions(conn, _P(file))
    typer.echo(f"approved {counts['approved']}, rejected {counts['rejected']}, "
               f"skipped {counts['skipped']}")


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
    send: bool = typer.Option(False, "--send",
                              help="actually send. Default writes Gmail drafts."),
    ignore_window: bool = typer.Option(False, "--ignore-window"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Draft approved, verified contacts into Gmail. Use --send to actually send.

    Drafting is the default because a message you have seen in the real client
    is a message you have actually reviewed. A draft is not a send: it does not
    touch the daily cap, does not mark the contact contacted, and does not start
    reply tracking or company suppression.
    """
    from .send_queue import main as send_main

    argv = ["--limit", str(limit), "--campaign", campaign]
    for flag, value in (("--mailbox", mailbox), ("--config", config), ("--db", db)):
        if value:
            argv += [flag, value]
    if dry_run:
        argv.append("--dry-run")
    if send:
        argv.append("--send")
    if ignore_window:
        argv.append("--ignore-window")
    raise typer.Exit(send_main(argv))


@app.command("drafts")
def drafts_cmd(
    campaign: Optional[str] = typer.Option(None, "--campaign"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """What is sitting in drafts, waiting on a human to press send."""
    from .send_queue import print_drafts

    conn = open_db(db)
    if print_drafts(conn):
        typer.echo("\nNone of these count as contacted. After sending them in Gmail:\n"
                   "  outbound mark-sent --all")


@app.command("mark-sent")
def mark_sent(
    message_ids: Optional[str] = typer.Option(None, "--ids", help="comma-separated"),
    all_drafts: bool = typer.Option(False, "--all"),
    config: Optional[str] = typer.Option(None, "--config"),
    db: Optional[str] = typer.Option(None, "--db"),
):
    """Record that drafts were sent by hand, and start the clock on them.

    This is the transition a draft never makes on its own. Detecting it
    automatically is unreliable: Gmail assigns its own Message-ID when you send
    a draft from the web client, so the id we generated at APPEND time is not
    necessarily the id that goes out, and matching on subject and recipient
    alone would mark the wrong thing on a resend. So this is explicit, and the
    honest cost is one command after sending.
    """
    cfg = _config(config)
    conn = open_db(db)
    where = "state = 'drafted'"
    params: tuple = ()
    if message_ids:
        ids = [int(x) for x in message_ids.split(",") if x.strip()]
        where += f" AND id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    elif not all_drafts:
        raise typer.BadParameter("pass --ids or --all")

    rows = conn.execute(f"SELECT * FROM messages WHERE {where}", params).fetchall()
    if not rows:
        typer.echo("no matching drafts")
        return
    with transaction(conn):
        for r in rows:
            conn.execute("UPDATE messages SET state='sent', sent_at=? WHERE id=?",
                         (utcnow(), r["id"]))
            conn.execute("UPDATE contacts SET status='active', updated_at=? WHERE id=?",
                         (utcnow(), r["contact_id"]))
            conn.execute("INSERT INTO mailbox_day (mailbox_id, day, messages, recipients)"
                         " VALUES (?,?,1,?) ON CONFLICT(mailbox_id, day) DO UPDATE SET"
                         " messages=messages+1, recipients=recipients+excluded.recipients",
                         (r["mailbox_id"], utcnow()[:10], r["recipient_count"]))
            typer.echo(f"  sent: {r['to_addr']}")

    # Sending is the claim. An explicit `outbound claim` exists, but the common
    # case must not depend on a habit anyone has to remember.
    from . import claims as C
    if cfg.campaign.claims_file:
        seen = set()
        for r in rows:
            for kind, value in ((C.PERSON, r["to_addr"]),
                                (C.COMPANY, _account_name_for(conn, r["contact_id"]))):
                if value and (kind, value.lower()) not in seen:
                    seen.add((kind, value.lower()))
                    C.add(cfg.campaign.claims_file, kind, value, note="sent")
        typer.echo(f"  recorded {len(seen)} claim(s) in "
                   f"{cfg.campaign.claims_file} -- push it so the group sees them")

    typer.secho(f"\n{len(rows)} marked sent. Reply tracking and suppression now apply.",
                fg=typer.colors.GREEN)


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
