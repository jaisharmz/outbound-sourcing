"""The people-graph: storage, dedup, path counting and scoring.

Everything here is mechanism. Which nodes are worth expanding, which edges are
worth following, and when a branch has gone off-target are judgment and belong
to the traversal layer, which is agentic.

The graph persists across runs. A person found today is a starting point
tomorrow, and the second run over an adjacent field is cheaper than the first.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import utcnow
from .normalize import normalize_person

NODE_KINDS = ("person", "organization", "lab", "paper")
EDGE_KINDS = ("coauthored_with", "advised_by", "advises", "lab_member_of", "works_at")


class GraphError(ValueError):
    pass


def person_key(name: str, external: dict | None = None) -> str:
    """Canonical identity. An OpenAlex id when we have one, else a normalized name."""
    if external and external.get("openalex"):
        return str(external["openalex"]).rsplit("/", 1)[-1]
    return normalize_person(name) or name.strip().lower()


def upsert_node(conn: sqlite3.Connection, kind: str, display_name: str, *,
                key: str | None = None, external: dict | None = None,
                attrs: dict | None = None, run_id: str | None = None) -> int:
    if kind not in NODE_KINDS:
        raise GraphError(f"unknown node kind {kind!r}; use one of {NODE_KINDS}")
    k = key or (person_key(display_name, external) if kind == "person"
                else re.sub(r"\s+", " ", display_name.strip().lower()))
    row = conn.execute("SELECT id, external_ids, attrs FROM graph_nodes"
                       " WHERE kind = ? AND key = ?", (kind, k)).fetchone()

    # A person met first through an OpenAlex id and later by name would otherwise
    # become two nodes -- the Albert Gu / Albert G. Gu shape. Adopt an existing
    # node with the same normalized name that has no external id yet, rather
    # than creating a second one. Two nodes that BOTH carry ids stay separate:
    # merging those is a judgment call and gets surfaced instead.
    if row is None and kind == "person":
        alt = conn.execute(
            "SELECT id, external_ids, attrs FROM graph_nodes"
            " WHERE kind = 'person' AND key = ?", (normalize_person(display_name),)
        ).fetchone()
        if alt and not json.loads(alt["external_ids"] or "{}"):
            conn.execute("UPDATE graph_nodes SET key = ? WHERE id = ?", (k, alt["id"]))
            row = alt

    if row:
        merged_ext = {**json.loads(row["external_ids"] or "{}"), **(external or {})}
        merged_attrs = {**json.loads(row["attrs"] or "{}"), **(attrs or {})}
        conn.execute("UPDATE graph_nodes SET external_ids = ?, attrs = ?, updated_at = ?"
                     " WHERE id = ?",
                     (json.dumps(merged_ext), json.dumps(merged_attrs), utcnow(), row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO graph_nodes (kind, key, display_name, external_ids, attrs,"
        " first_seen_run, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (kind, k, display_name.strip(), json.dumps(external or {}),
         json.dumps(attrs or {}), run_id, utcnow(), utcnow()))
    return int(cur.lastrowid)


def add_edge(conn: sqlite3.Connection, src_id: int, dst_id: int, kind: str, *,
             source_url: str, year: int | None = None, quote: str | None = None,
             paper_key: str | None = None, is_current: bool | None = None) -> bool:
    """Record a relationship. Refuses an edge with no source: an unsourced
    relationship is an assertion, and assertions do not get emailed."""
    if kind not in EDGE_KINDS:
        raise GraphError(f"unknown edge kind {kind!r}; use one of {EDGE_KINDS}")
    if not source_url or not source_url.startswith("http"):
        raise GraphError(f"edge {kind!r} needs an absolute source URL, got {source_url!r}")
    if src_id == dst_id:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO graph_edges (src_id, dst_id, kind, year, is_current,"
        " paper_key, source_url, quote, retrieved_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (src_id, dst_id, kind, year,
         None if is_current is None else int(is_current),
         paper_key or "", source_url, quote, utcnow(), utcnow()))
    return cur.rowcount > 0


def record_path(conn: sqlite3.Connection, node_id: int, run_id: str, *,
                seed_node_id: int | None, hops: int, via: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO graph_paths (node_id, run_id, seed_node_id, hops, via,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (node_id, run_id, seed_node_id, hops, via, utcnow()))


def path_count(conn: sqlite3.Connection, node_id: int) -> int:
    """Independent routes to a node. Three routes means more central than one."""
    return conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(seed_node_id,0) || '|' || via)"
        " FROM graph_paths WHERE node_id = ?", (node_id,)).fetchone()[0]


def degree(conn: sqlite3.Connection, node_id: int, kind: str | None = None) -> int:
    q = ("SELECT COUNT(*) FROM graph_edges WHERE (src_id = ? OR dst_id = ?)"
         + (" AND kind = ?" if kind else ""))
    params = (node_id, node_id) + ((kind,) if kind else ())
    return conn.execute(q, params).fetchone()[0]


def log_expansion(conn: sqlite3.Connection, run_id: str, node_id: int | None,
                  decision: str, reason: str, yielded: int = 0) -> None:
    conn.execute("INSERT INTO graph_expansions (run_id, node_id, decision, reason,"
                 " yielded, created_at) VALUES (?,?,?,?,?,?)",
                 (run_id, node_id, decision, reason, yielded, utcnow()))


# ------------------------------------------------------------------ scoring


@dataclass
class Score:
    total: float
    parts: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


SENIORITY_BAND = {
    # The target is mid-career: past the first-author-only stage, not yet running
    # a lab with two hundred coauthors and no time.
    "phd_student": 0.35, "first_year": 0.10, "postdoc": 0.80,
    "research_scientist": 1.00, "senior_researcher": 1.00, "staff": 0.95,
    "principal": 0.90, "founder": 0.85, "pi": 0.25, "professor": 0.25,
    "unknown": 0.55,
}

HUB_DEGREE = 60          # beyond this a node is a hub, not a lead


def score_node(conn: sqlite3.Connection, node_id: int, *, hops: int,
               topic_match: float = 0.5, latest_year: int | None = None,
               email_resolvable: bool = False, seniority: str = "unknown") -> Score:
    """Rank, never filter to one shape. Every component is explainable."""
    now = datetime.now(timezone.utc).year
    paths = path_count(conn, node_id)
    deg = degree(conn, node_id)

    parts = {
        # Closer is better, but a 2-hop with three routes beats a 1-hop with one.
        "proximity": 1.0 / (1 + hops),
        "corroboration": min(1.0, math.log1p(paths) / math.log(4)),
        "topic": max(0.0, min(1.0, topic_match)),
        "recency": 0.0 if not latest_year else max(0.0, 1 - (now - latest_year) / 10),
        "reachable": 1.0 if email_resolvable else 0.0,
        "seniority": SENIORITY_BAND.get(seniority, SENIORITY_BAND["unknown"]),
    }
    # Reweighted after measuring the components on a real run. Topic scoring by
    # keyword against OpenAlex labels returned 0.00 for people who were plainly
    # relevant, and a scorer that is wrong is worse than one that is absent, so
    # `topic` is now overlap with the seed's own topics -- no term list to be
    # wrong about -- and it is weighted below the two components that measurably
    # discriminate. path_count and recency are the real signal; the rest is
    # tie-breaking until affiliation is resolved, at which point seniority and
    # company fit become real and this should be revisited.
    weights = {"proximity": 0.8, "corroboration": 2.0, "topic": 0.9,
               "recency": 1.6, "reachable": 0.6, "seniority": 1.0}
    total = sum(parts[k] * weights[k] for k in parts) / sum(weights.values())

    notes = []
    if deg > HUB_DEGREE:
        penalty = min(0.45, 0.15 * math.log1p(deg / HUB_DEGREE))
        total -= penalty
        notes.append(f"hub penalty -{penalty:.2f} ({deg} edges): expanding this reaches "
                     f"everyone and distinguishes no one")
    if paths >= 3:
        notes.append(f"reached by {paths} independent routes")
    if seniority in ("pi", "professor"):
        notes.append("PI band: reachable and senior, but rarely the person who does the work")
    if seniority in ("phd_student", "first_year"):
        notes.append("early-career band: often the right topic, rarely the right authority")
    return Score(round(max(0.0, total), 3), {k: round(v, 3) for k, v in parts.items()}, notes)
