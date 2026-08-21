"""What is wrong, and the exact command or click that fixes it.

This is what someone runs when nothing works, so it diagnoses the failures this
project actually hit rather than checking that YAML parses. Every check that can
fail prints a fix. A diagnosis without a fix just relocates the confusion.

Ordering matters: checks run cheapest-first and stop-worthy problems appear
first, because the person reading this is already frustrated and will act on the
first thing they see.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: list[str] = field(default_factory=list)


def _c(name, status, detail="", fix=()) -> Check:
    return Check(name, status, detail, list(fix))


# ----------------------------------------------------------------- checks


# Shells read different files, and which one they read depends on whether the
# shell is a login shell. This is the reason a fix can be correctly applied and
# still appear not to work.
PROFILES = {
    "zsh": ["~/.zshrc", "~/.zprofile", "~/.zshenv"],
    "bash": ["~/.bashrc", "~/.bash_profile", "~/.profile"],
    "fish": ["~/.config/fish/config.fish"],
}


def check_path() -> Check:
    """The console script has to be on PATH or nothing else in the docs works.

    Reports which shell and which files were inspected, because "you never added
    it" and "you added it to a file this shell does not read" need different
    fixes and look identical from the error message alone. Being told to apply a
    fix you already applied is worse than no message.
    """
    venv_bin = Path(sys.executable).parent
    found = shutil.which("outbound")
    shell = Path(os.environ.get("SHELL", "")).name or "unknown"
    candidates = PROFILES.get(shell, sorted({f for v in PROFILES.values() for f in v}))

    mentions = []
    for rel in candidates:
        f = Path(rel).expanduser()
        if f.exists() and str(venv_bin) in f.read_text():
            mentions.append(rel)

    checked = f"shell={shell}; checked {', '.join(candidates)}"
    if found:
        return _c("console script on PATH", OK, f"{found}  ({checked})")

    if mentions:
        # Applied, but not visible here. Almost always a non-interactive or
        # non-login shell, which is also how the installer and any editor
        # terminal run.
        return _c("console script on PATH", WARN,
                  f"`outbound` is not on PATH in THIS shell, but {', '.join(mentions)} "
                  f"already adds it. This shell did not read that file "
                  f"({checked})",
                  ["open a new interactive terminal and re-run: outbound doctor",
                   f"if you are in an editor or script, use the full path: "
                   f"{venv_bin}/outbound",
                   f"to make it work in non-login shells too, add the same line to "
                   f"{'~/.zshenv' if shell == 'zsh' else '~/.profile'}"])

    return _c("console script on PATH", FAIL,
              f"`outbound` is not on your PATH and no profile file adds it, so every "
              f"command in the docs will say 'command not found' ({checked})",
              [f'echo \'export PATH="{venv_bin}:$PATH"\' >> {candidates[0]}',
               f"then open a new terminal, or run: source {candidates[0]}",
               f"or use the full path: {venv_bin}/outbound"])


def check_dnspython() -> Check:
    """The silent one: without it every address verifies as unknown."""
    try:
        import dns.resolver  # noqa: F401
    except ImportError:
        return _c("dnspython installed", FAIL,
                  "MX lookup cannot run at all, so no address can be verified. "
                  "This fails quietly -- verification returns 'unknown' for "
                  "everything rather than erroring",
                  ["pip install dnspython",
                   "or re-run ./install.sh, which installs it"])
    try:
        import dns.resolver
        r = dns.resolver.Resolver()
        r.lifetime = r.timeout = 5
        r.resolve("gmail.com", "MX")
    except Exception as exc:
        return _c("dnspython installed", WARN,
                  f"installed, but a test lookup for gmail.com failed "
                  f"({type(exc).__name__}). DNS may be blocked here; addresses "
                  f"will verify as unknown rather than invalid",
                  ["check your network or VPN, then re-run: outbound doctor"])
    return _c("dnspython installed", OK, "MX lookups work")


def check_config(root: Path) -> Check:
    from .config import Config
    from .errors import ConfigError

    if not root.exists():
        return _c("config loads", FAIL, f"no config directory at {root}",
                  ["cp -r config.example config",
                   "then edit config/persona.md with your details"])
    try:
        Config(root)
    except ConfigError as exc:
        first = str(exc).splitlines()[0]
        return _c("config loads", FAIL, first,
                  ["fix the file named above",
                   "then re-run: outbound doctor"])
    return _c("config loads", OK)


def check_secrets(root: Path) -> Check:
    """Presence only. Values are never printed, here or anywhere."""
    path = root / "secrets.env"
    if not path.exists():
        return _c("secrets.env present", FAIL, f"no {path}",
                  ["cp config.example/secrets.env.example config/secrets.env",
                   "then fill in GMAIL_APP_PASSWORD and GITHUB_TOKEN"])
    present = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            present[k.strip()] = bool(v.strip())
    # Which key is required is a config question, not a constant. Hardcoding one
    # name meant this reported the developer's key as missing on a fresh install
    # that correctly used the example's.
    required = []
    try:
        from .config import Config

        for mb in Config(root).mailboxes.enabled():
            ref = getattr(mb, "auth_ref", None)
            if ref:
                required.append(ref)
    except Exception:
        pass

    missing = [k for k in required if not present.get(k)]
    if missing:
        return _c("secrets.env present", FAIL,
                  f"{', '.join(missing)} is empty, and the enabled mailbox needs it "
                  f"to send or draft anything",
                  ["enable 2-Step Verification on the Google account",
                   "create an app password: myaccount.google.com/apppasswords",
                   "paste the 16 characters into config/secrets.env"])
    if not present.get("GITHUB_TOKEN"):
        return _c("secrets.env present", WARN,
                  "GITHUB_TOKEN is empty. The domain-pattern channel is the main "
                  "address source for companies that do not publish, and it is "
                  "rate-limited to 60 requests an hour without a token",
                  ["create a fine-grained token with NO scopes selected:",
                   "  github.com/settings/personal-access-tokens/new",
                   "public read is the default and is all this needs",
                   "paste it into config/secrets.env as GITHUB_TOKEN"])
    return _c("secrets.env present", OK, f"{sum(present.values())} value(s) set")


def check_mailbox(root: Path) -> Check:
    """Authenticate for real. A wrong app password looks identical to a right one
    until the first send."""
    from .config import Config
    from . import providers

    try:
        cfg = Config(root)
        enabled = cfg.mailboxes.enabled()
    except Exception as exc:
        return _c("mailbox authenticates", FAIL, f"config error: {exc}", [])
    if not enabled:
        return _c("mailbox authenticates", FAIL, "no mailbox has enabled: true",
                  ["set enabled: true on one mailbox in config/mailboxes.yaml"])
    mb = enabled[0]
    try:
        ok, detail = providers.build(mb, cfg.secrets()).verify_auth()
    except Exception as exc:
        return _c("mailbox authenticates", FAIL, f"{type(exc).__name__}: {exc}", [])
    if ok:
        return _c("mailbox authenticates", OK, f"{mb.id} ({mb.from_.address})")
    return _c("mailbox authenticates", FAIL, detail,
              ["a 535 error means the app password is wrong, or 2-Step "
               "Verification is not enabled on the account",
               "app passwords require 2FA: "
               "myaccount.google.com/signinoptions/two-step-verification",
               "then regenerate: myaccount.google.com/apppasswords"])


def check_github(root: Path) -> Check:
    from .config import Config
    from .github_harvest import Client

    try:
        token = Config(root).secrets().get("GITHUB_TOKEN")
    except Exception:
        token = None
    if not token:
        return _c("github api reachable", WARN, "no token; skipping",
                  ["see the secrets.env check above"])
    payload, status = Client(token=token).get("/rate_limit")
    if status == "throttled" or (payload and
                                 payload.get("resources", {}).get("core", {})
                                 .get("remaining", 1) == 0):
        return _c("github api reachable", FAIL,
                  "rate limit exhausted. The domain-pattern channel will find "
                  "nothing and report it as an honest zero",
                  ["wait for the reset, or check the token is actually being sent",
                   "verify with: curl -H \"Authorization: Bearer $TOKEN\" "
                   "https://api.github.com/rate_limit"])
    if status != "ok":
        return _c("github api reachable", FAIL, f"status {status}",
                  ["the token may be expired or malformed",
                   "create a new fine-grained token with no scopes"])
    remaining = payload["resources"]["core"]["remaining"]
    return _c("github api reachable", OK, f"{remaining} requests remaining this hour")


def check_database(root: Path) -> Check:
    from .db import default_db_path, migrate

    path = default_db_path()
    if not path.exists():
        return _c("database migrated", FAIL, f"no database at {path}",
                  ["outbound db migrate"])
    try:
        conn = sqlite3.connect(path)
        applied = {r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations")}
    except sqlite3.Error as exc:
        return _c("database migrated", FAIL, f"unreadable: {exc}",
                  ["outbound db migrate"])
    on_disk = {p.name for p in (Path(__file__).parent / "migrations").glob("*.sql")}
    pending = sorted(on_disk - applied)
    if pending:
        return _c("database migrated", FAIL,
                  f"{len(pending)} migration(s) not applied: {pending[:3]}",
                  ["outbound db migrate"])
    return _c("database migrated", OK, f"{len(applied)} migration(s) applied")


def check_attachments(root: Path) -> Check:
    from .config import Config, human, wire_size
    from .templates import resolve_documents

    try:
        cfg = Config(root)
    except Exception as exc:
        return _c("attachments fit", FAIL, str(exc).splitlines()[0], [])
    problems = []
    for step in cfg.sequence.steps:
        if not step.attachment_set:
            continue
        try:
            attachments, _ = resolve_documents(cfg, step)
        except Exception as exc:
            problems.append(f"{step.id}: {exc}")
            continue
        total = wire_size(sum(a.size for a in attachments))
        if total > cfg.campaign.campaign_max_attachment_bytes:
            problems.append(
                f"{step.id} would attach {human(total)}, over the "
                f"{human(cfg.campaign.campaign_max_attachment_bytes)} cap")
    if problems:
        return _c("attachments fit", FAIL, "; ".join(problems[:2]),
                  ["compress the file, or give the document a `url:` in "
                   "config/sequence.yaml so it is linked instead of attached",
                   "base64 inflates attachments by 4/3 -- the cap is on the "
                   "encoded size, which is what gateways measure"])
    return _c("attachments fit", OK)


def check_links(root: Path) -> Check:
    from .check_links import check_url, current_urls
    from .config import Config

    try:
        urls = current_urls(Config(root))
    except Exception as exc:
        return _c("linked documents open", FAIL, str(exc).splitlines()[0], [])
    if not urls:
        return _c("linked documents open", OK, "no linked documents")
    from .check_links import is_placeholder

    placeholders = [u for u in urls if is_placeholder(u)]
    if placeholders:
        return _c("linked documents open", WARN,
                  f"{len(placeholders)} link(s) are still the example URLs from "
                  f"config.example",
                  ["replace them in config/sequence.yaml with your own documents",
                   "a campaign will not start while an example URL remains"])
    bad = []
    for url in sorted(urls):
        status, detail = check_url(url)
        if status != "ok":
            bad.append(f"{url[:48]}: {status}")
    if bad:
        return _c("linked documents open", FAIL, "; ".join(bad[:2]),
                  ["a Drive link must be shared as 'Anyone with the link'",
                   "use the direct-download form, not /view?usp=sharing:",
                   "  https://drive.google.com/uc?export=download&id=<FILE_ID>",
                   "a request-access wall is worse than no link at all"])
    return _c("linked documents open", OK, f"{len(urls)} link(s) reachable")


def check_template_hash(root: Path) -> Check:
    """Copy edited since the last test send is the most common day-two failure."""
    from .config import Config
    from .db import default_db_path
    from . import templates

    try:
        cfg = Config(root)
    except Exception as exc:
        return _c("templates match the last test send", FAIL,
                  str(exc).splitlines()[0], [])
    if not default_db_path().exists():
        return _c("templates match the last test send", WARN, "no database yet",
                  ["outbound db migrate"])
    conn = sqlite3.connect(default_db_path())
    conn.row_factory = sqlite3.Row
    stale = []
    for name in cfg.campaigns.campaigns:
        # A campaign already blocked by its own placeholder cannot send whatever
        # its hash says, so reporting it here is noise on top of a problem the
        # operator already knows about.
        if cfg.preflight("campaign", campaign=name):
            continue
        want = templates.template_hash(cfg, name)
        row = conn.execute(
            "SELECT template_hash FROM test_sends WHERE campaign=? AND ok=1"
            " ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        if row is None:
            stale.append(f"{name}: never test-sent")
        elif row["template_hash"] != want:
            stale.append(f"{name}: copy changed since the last test send")
    if stale:
        return _c("templates match the last test send", FAIL, "; ".join(stale[:2]),
                  ["outbound test-email --mailbox <id> --campaign <name>",
                   "this exists so an edit to the copy cannot reach strangers "
                   "without you having seen the rendered result once"])
    return _c("templates match the last test send", OK)


def run(root: Path) -> list[Check]:
    checks = [check_path(), check_dnspython(), check_config(root)]
    if checks[-1].status == FAIL:
        return checks              # nothing below can run without config
    checks += [check_secrets(root), check_mailbox(root), check_github(root),
               check_database(root), check_attachments(root), check_links(root),
               check_template_hash(root)]
    return checks
