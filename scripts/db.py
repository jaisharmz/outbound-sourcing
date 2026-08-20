"""SQLite access and forward-only migrations.

The database is the single source of truth. Discovery hands it JSON through
`ingest_candidates.py`; everything after that reads here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent / "state" / "prospects.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_db_path()
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
