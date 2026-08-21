"""People the operator already knows, and everyone one hop from them.

Distinct from suppression, which records opt-outs and bounces and is about what
the recipient asked for. This is about what the operator already has: a personal
relationship that a cold sequence would be the wrong instrument for. Sending a
templated pitch to your own advisor's student is worse than not sending at all.

Transitive, but exactly one hop. Dawn Song's students and current collaborators
are people the operator plausibly knows through her; their coauthors are not,
and expanding further would exclude most of the field. The hop is taken over the
graph, so every exclusion has the same evidence backing as the edge that caused
it -- an absolute source URL and a quote.

Which edges carry a personal relationship, and why:

  advised_by / advises   Always, any year. An advising relationship does not
                         expire; a former student is still someone you know.
  lab_member_of          Always. Membership of an excluded lab is the exclusion.
  coauthored_with        Only within COLLABORATION_RECENCY_YEARS. The operator
                         said "current collaborators", and a paper from 2011 is
                         not a current relationship -- it is a fact about the
                         past that both parties may have forgotten.
  works_at               Never. A shared employer is not a personal tie; that is
                         what company-level suppression is for.

Failure direction is deliberate: over-exclude. Dropping a good prospect costs
one name from a list. Cold-emailing someone the operator sees at group meeting
costs the relationship and the operator's credibility.
"""

from __future__ import annotations

import datetime
import sqlite3

from .config import Config
from .db import log_event, utcnow

COLLABORATION_RECENCY_YEARS = 5

# Edge kinds that transmit exclusion, and whether recency gates them.
PERSONAL_EDGES: dict[str, bool] = {
    "advised_by": False,
    "advises": False,
    "lab_member_of": False,
    "coauthored_with": True,
}


def _seeds(conn: sqlite3.Connection, config: Config) -> list[tuple[int, str, str]]:
    """Nodes named directly in personal_exclusions.yaml. (node_id, name, why)."""
    px = config.personal_exclusions
    found: list[tuple[int, str, str]] = []

    for row in conn.execute("SELECT id, kind, display_name FROM graph_nodes"):
        name, kind = row["display_name"], row["kind"]
        if kind == "person" and px.excluded_person(name):
            found.append((row["id"], name, "named in personal_exclusions.yaml (people)"))
            continue
        lab = px.excluded_lab(name)
        if lab and kind in ("lab", "person"):
            found.append((row["id"], name,
                          f"matches excluded lab {lab.name!r}"
                          + (f": {lab.reason.strip()}" if lab.reason else "")))
            continue
        if kind == "organization" and any(
                c.strip().lower() == name.strip().lower() for c in px.companies):
            found.append((row["id"], name, "named in personal_exclusions.yaml (companies)"))
    return found


def compute(conn: sqlite3.Connection, config: Config) -> dict[int, dict]:
    """Every excluded node id -> why, one hop out from the named seeds."""
    cutoff = datetime.date.today().year - COLLABORATION_RECENCY_YEARS
    excluded: dict[int, dict] = {}

    for node_id, name, why in _seeds(conn, config):
        excluded[node_id] = {"name": name, "reason": why, "hops": 0,
                             "via": None, "source_url": None, "through": None}

    for seed_id in list(excluded):
        seed = excluded[seed_id]["name"]
        for row in conn.execute(
            "SELECT e.kind, e.year, e.source_url, e.src_id, e.dst_id,"
            "       ns.display_name AS src_name, nd.display_name AS dst_name,"
            "       ns.kind AS src_kind, nd.kind AS dst_kind"
            "  FROM graph_edges e"
            "  JOIN graph_nodes ns ON ns.id = e.src_id"
            "  JOIN graph_nodes nd ON nd.id = e.dst_id"
            " WHERE e.src_id = ? OR e.dst_id = ?", (seed_id, seed_id)
        ):
            if row["kind"] not in PERSONAL_EDGES:
                continue
            if PERSONAL_EDGES[row["kind"]] and (row["year"] or 0) < cutoff:
                continue
            other_id = row["dst_id"] if row["src_id"] == seed_id else row["src_id"]
            other_kind = row["dst_kind"] if row["src_id"] == seed_id else row["src_kind"]
            other_name = row["dst_name"] if row["src_id"] == seed_id else row["src_name"]
            if other_kind != "person" or other_id in excluded:
                continue
            year = f" ({row['year']})" if row["year"] else ""
            excluded[other_id] = {
                "name": other_name,
                "reason": f"{row['kind'].replace('_', ' ')} {seed}{year}",
                "hops": 1, "via": row["kind"], "through": seed,
                "source_url": row["source_url"],
            }
    return excluded


def refresh(conn: sqlite3.Connection, config: Config) -> dict[int, dict]:
    """Recompute and persist, so what was excluded is reviewable after the fact."""
    excluded = compute(conn, config)
    conn.execute("DELETE FROM exclusions_applied")
    for node_id, e in excluded.items():
        conn.execute(
            "INSERT INTO exclusions_applied (node_id, name, reason, hops, via,"
            " through, source_url, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            (node_id, e["name"], e["reason"], e["hops"], e["via"], e["through"],
             e["source_url"], utcnow()))
    log_event(conn, "info", "exclusions.refresh", total=len(excluded),
              seeds=sum(1 for e in excluded.values() if e["hops"] == 0))
    return excluded


def excluded_names(conn: sqlite3.Connection) -> dict[str, dict]:
    """Lowercased name -> record, for checking candidates that have no node yet."""
    return {r["name"].strip().lower(): dict(r)
            for r in conn.execute("SELECT * FROM exclusions_applied")}


def check(conn: sqlite3.Connection, config: Config, name: str,
          lab: str | None = None, company: str | None = None) -> dict | None:
    """Is this person excluded? Checked by name, by lab, and by company.

    Name matching is exact on the normalized string. The graph hop is what makes
    this transitive; fuzzy name matching here would mostly produce false
    exclusions on common surnames, which is over-excluding for no evidence.
    """
    px = config.personal_exclusions
    n = (name or "").strip().lower()

    hit = excluded_names(conn).get(n)
    if hit:
        return hit
    if px.excluded_person(name):
        return {"name": name, "reason": "named in personal_exclusions.yaml", "hops": 0}
    for text, kind in ((lab, "lab"), (company, "affiliation")):
        if text and (m := px.excluded_lab(text)):
            return {"name": name, "hops": 0,
                    "reason": f"{kind} {text!r} matches excluded lab {m.name!r}"}
    if company and any(c.strip().lower() == company.strip().lower() for c in px.companies):
        return {"name": name, "hops": 0,
                "reason": f"employer {company!r} is in personal_exclusions.yaml"}
    return None
