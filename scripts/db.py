"""SQLite access and forward-only migrations.

The database is the single source of truth. Discovery hands it JSON through
`ingest_candidates.py`; everything after that reads here.
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "state" / "prospects.db"


def scratch_db_path(name: str) -> Path:
    """A named database that is deliberately not production.

    Pipeline validation and demos write here. Campaign inventory and a run used
    to exercise the machinery should not share a table.
    """
    return Path(__file__).resolve().parent.parent / "state" / f"{name}.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProductionDatabaseError(RuntimeError):
    """A non-production caller tried to open the real database."""


# Console scripts that are allowed to touch production. Anything else has to
# say so out loud.
SANCTIONED_ENTRYPOINTS = ("outbound",)


def _entrypoint() -> tuple[bool, str]:
    """Is this a real CLI invocation, or something ad-hoc?

    `python -m scripts.outbound` and the `outbound` console script are the
    sanctioned ways in. `python -c`, a REPL, a scratch script and pytest are not.
    """
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", "") or ""
    if name.startswith("scripts."):
        return True, f"python -m {name}"
    argv0 = Path(sys.argv[0]).name if sys.argv else ""
    if argv0 in SANCTIONED_ENTRYPOINTS:
        return True, f"{argv0} CLI"
    if argv0 in ("-c", "") or argv0.startswith("-"):
        return False, "an inline `python -c` script"
    if "pytest" in argv0:
        return False, "pytest"
    return False, f"{argv0 or 'an unknown caller'}"


def _guard_production(p: Path) -> None:
    """Refuse to open the production database from a context that must not.

    Default-deny, not opt-in. The first version only fired under pytest or when
    OUTBOUND_NO_PROD_DB was set, which meant an ad-hoc `python -c` -- the exact
    thing people reach for while checking something -- sailed straight through
    and wrote a junk row into real data. That is the same shape as `demo`
    defaulting to production and seeding three fixture companies into it.

    Twice is a pattern, so the rule is inverted: only the CLI entrypoints may
    open production. Everything else names a scratch path, sets OUTBOUND_DB, or
    sets OUTBOUND_ALLOW_PROD=1 to say deliberately that it means production.
    """
    try:
        if p.resolve() != default_db_path().resolve():
            return
    except OSError:
        return
    if os.environ.get("OUTBOUND_ALLOW_PROD") == "1":
        return
    if os.environ.get("OUTBOUND_NO_PROD_DB"):
        raise ProductionDatabaseError(
            f"refusing to open the production database at {p} "
            f"(OUTBOUND_NO_PROD_DB is set). Point at a scratch database instead: "
            f"set OUTBOUND_DB=/path/to/scratch.db or pass an explicit path."
        )
    sanctioned, who = _entrypoint()
    if not sanctioned:
        raise ProductionDatabaseError(
            f"refusing to open the production database at {p}.\n"
            f"  caller: {who}\n"
            f"  Only the outbound CLI may open production. For a scratch run set "
            f"OUTBOUND_DB=/path/to/scratch.db or pass an explicit path.\n"
            f"  If you genuinely mean production from here, set OUTBOUND_ALLOW_PROD=1 "
            f"-- and be aware that writes will be real."
        )


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_db_path()
    _guard_production(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # a reader during a send is normal
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, verbose: bool = False) -> list[str]:
    """Apply every unapplied migration in filename order. Idempotent."""
    done = _applied(conn)
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        # Each migration is one transaction: a half-applied schema is worse than
        # a failed startup. The BEGIN/COMMIT go inside the script because
        # executescript commits any transaction that is already open.
        version = path.name.replace("'", "''")
        script = (
            "BEGIN;\n"
            + path.read_text()
            + f"\nINSERT INTO schema_migrations (version, applied_at)"
            f" VALUES ('{version}', '{utcnow()}');\nCOMMIT;\n"
        )
        try:
            conn.executescript(script)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass  # executescript may have already rolled back
            raise
        applied.append(path.name)
        if verbose:
            print(f"applied {path.name}")
    return applied


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database. OUTBOUND_DB redirects every command at once.

    Passing --db to each command in a test run is a step that can be forgotten
    halfway through, and the failure is silent: the run works and quietly writes
    into the production queue. One environment variable is harder to half-apply.
    """
    import os

    path = path or os.environ.get("OUTBOUND_DB") or None
    conn = connect(path)
    migrate(conn)
    return conn


def log_event(conn: sqlite3.Connection, level: str, event: str, **payload: Any) -> None:
    conn.execute(
        "INSERT INTO events (ts, level, event, payload) VALUES (?,?,?,?)",
        (utcnow(), level, event, json.dumps(payload, default=str)),
    )


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    cur = conn.execute(sql, params)
    return cur.fetchone()


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = one(conn, sql, params)
    return row[0] if row else None


def get_or_create_campaign(conn: sqlite3.Connection, name: str) -> int:
    row = one(conn, "SELECT id FROM campaigns WHERE name = ?", (name,))
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO campaigns (name, created_at) VALUES (?,?)", (name, utcnow())
    )
    return int(cur.lastrowid)


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = connect(target)
    applied = migrate(conn, verbose=True)
    print(f"database ready at {target or default_db_path()}; {len(applied)} migration(s) applied")
