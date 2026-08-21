"""Traversal mechanics: expand a node, store what came back, render the map.

The judgment -- which nodes justify expansion, which edges to follow, when a
branch has gone off-target, when marginal yield has collapsed -- is the model's
and lives in the run plan, not here. This module makes those decisions cheap to
execute and impossible to make without leaving a record.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import graph as G
from .db import utcnow
from .openalex import Client, Work, coauthor_counts


def plan_path(run_id: str) -> Path:
    p = Path(__file__).resolve().parent.parent / "state" / "graph"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"plan_{run_id}.md"


@dataclass
class Expansion:
    node_id: int
    name: str
    added_people: int = 0
    added_edges: int = 0
    coauthors: dict = field(default_factory=dict)


def seed_person(conn: sqlite3.Connection, client: Client, name: str, run_id: str, *,
                affiliation_hint: str | None = None) -> tuple[int, dict] | None:
    a = client.find_author(name, affiliation_hint=affiliation_hint)
    if not a:
        return None
    nid = G.upsert_node(conn, "person", a.name, external={"openalex": a.short_id},
                        attrs={"works": a.works_count, "cited": a.cited_by_count,
                               "topics": a.topics,
                               "institution": a.last_known_institution},
                        run_id=run_id)
    G.record_path(conn, nid, run_id, seed_node_id=None, hops=0, via="seed")
    if a.last_known_institution:
        oid = G.upsert_node(conn, "organization", a.last_known_institution, run_id=run_id)
        G.add_edge(conn, nid, oid, "works_at", source_url=a.url, is_current=True,
                   quote=f"OpenAlex last known institution: {a.last_known_institution}")
    return nid, {"author": a}


def expand(conn: sqlite3.Connection, client: Client, node_id: int, run_id: str, *,
           seed_node_id: int, hops: int, via: str, since: int | None = None,
           min_papers: int = 1) -> Expansion:
    """Pull a person's coauthors from OpenAlex and record them as graph edges."""
    row = conn.execute("SELECT display_name, external_ids FROM graph_nodes WHERE id = ?",
                       (node_id,)).fetchone()
    ext = json.loads(row["external_ids"] or "{}")
    oa = ext.get("openalex")
    exp = Expansion(node_id=node_id, name=row["display_name"])
    if not oa:
        return exp

    works = client.works(oa, since=since)
    counts = coauthor_counts(works, oa)
    exp.coauthors = counts

    for short, info in counts.items():
        if info["count"] < min_papers:
            continue
        inst = info["institutions"][0] if info["institutions"] else None
        cid = G.upsert_node(conn, "person", info["name"],
                            external={"openalex": short},
                            attrs={"institution": inst,
                                   "papers_with_source": info["count"],
                                   "latest_year": info["latest"]},
                            run_id=run_id)
        title, year, url = info["papers"][0]
        if G.add_edge(conn, node_id, cid, "coauthored_with", source_url=url,
                      year=info["latest"], paper_key=url.rsplit("/", 1)[-1],
                      quote=f"co-authored \"{title}\" ({year})"):
            exp.added_edges += 1
        G.record_path(conn, cid, run_id, seed_node_id=seed_node_id, hops=hops,
                      via=via + ">coauthored_with")
        if inst:
            oid = G.upsert_node(conn, "organization", inst, run_id=run_id)
            G.add_edge(conn, cid, oid, "works_at", source_url=f"https://openalex.org/{short}",
                       is_current=True, quote=f"OpenAlex affiliation on a shared paper: {inst}")
        exp.added_people += 1
    return exp


# ------------------------------------------------------------------ map


def render_map(conn: sqlite3.Connection, run_id: str) -> str:
    """A readable account of the graph this run built."""
    lines = [f"# Graph map — run `{run_id}`", ""]

    seeds = conn.execute("""SELECT n.id, n.display_name FROM graph_paths p
                            JOIN graph_nodes n ON n.id = p.node_id
                            WHERE p.run_id = ? AND p.hops = 0""", (run_id,)).fetchall()
    lines.append("## Seeds")
    lines.append("")
    for s in seeds:
        lines.append(f"- **{s['display_name']}**")
    lines.append("")

    lines.append("## Expansion decisions")
    lines.append("")
    for e in conn.execute("""SELECT e.decision, e.reason, e.yielded, n.display_name
                             FROM graph_expansions e
                             LEFT JOIN graph_nodes n ON n.id = e.node_id
                             WHERE e.run_id = ? ORDER BY e.id""", (run_id,)):
        mark = "expanded" if e["decision"] == "expanded" else "skipped "
        who = e["display_name"] or "(run)"
        lines.append(f"- `{mark}` **{who}** — {e['reason']}"
                     + (f" _(+{e['yielded']} people)_" if e["yielded"] else ""))
    lines.append("")

    lines.append("## Where each person sits")
    lines.append("")
    rows = conn.execute("""
        SELECT n.id, n.display_name, n.attrs, MIN(p.hops) hops,
               COUNT(DISTINCT COALESCE(p.seed_node_id,0) || '|' || p.via) routes
          FROM graph_paths p JOIN graph_nodes n ON n.id = p.node_id
         WHERE p.run_id = ? AND n.kind = 'person'
         GROUP BY n.id ORDER BY hops, routes DESC, n.display_name""", (run_id,)).fetchall()
    for r in rows:
        a = json.loads(r["attrs"] or "{}")
        inst = a.get("institution") or "affiliation unknown"
        extra = []
        if a.get("papers_with_source"):
            extra.append(f"{a['papers_with_source']} shared paper(s)")
        if a.get("latest_year"):
            extra.append(f"latest {a['latest_year']}")
        if r["routes"] > 1:
            extra.append(f"{r['routes']} routes")
        lines.append(f"- {'·' * r['hops']} **{r['display_name']}** — {inst}"
                     + (f"  ({', '.join(extra)})" if extra else ""))
    lines.append("")
    lines.append(f"_{len(rows)} people, "
                 f"{conn.execute('SELECT COUNT(*) FROM graph_edges').fetchone()[0]} edges "
                 f"in the graph overall._")
    return "\n".join(lines)
