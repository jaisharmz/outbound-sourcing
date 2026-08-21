"""Traversal mechanics: expand a node, store what came back, render the map.

The judgment -- which nodes justify expansion, which edges to follow, when a
branch has gone off-target, when marginal yield has collapsed -- is the model's
and lives in the run plan, not here. This module makes those decisions cheap to
execute and impossible to make without leaving a record.
"""

from __future__ import annotations

import json
import re
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
    resolve_affiliation(conn, client, nid, a, run_id)
    return nid, {"author": a}


# Company aliases, because the string an author types is not the company's
# trade name. Anything not listed falls back to the name itself.
COMPANY_PATTERNS = {
    "together ai": r"together\s*ai|together\.ai|togethercomputer",
    "fireworks ai": r"fireworks\s*ai|fireworks\.ai",
    "baseten": r"baseten",
    "groq": r"\bgroq\b",
    "etched": r"\betched\b",
    "cursor": r"\bcursor\b|\banysphere\b",
}

# Different companies that share a name. "Cursor Insight Ltd." is a London
# handwriting-analytics firm with a real, current, correctly-formatted
# affiliation line -- it passed the founding-year and prose guards cleanly and
# produced eight confident false positives. No heuristic separates it from
# Anysphere's Cursor; only knowing they are different companies does.
EXCLUDE_PATTERNS = {
    "cursor": r"cursor\s+insight",
}

# Founding years. A company cannot have employed anyone before it existed, and
# this is the cheapest guard against a name that is also an ordinary English
# word: "Etched" matched the verb in semiconductor abstracts back to 1991, and
# "Cursor" matched an unrelated Chilean "Cursor Ltd." and Spanish ecology prose.
FOUNDED = {
    "together ai": 2022, "fireworks ai": 2022, "baseten": 2019,
    "groq": 2016, "etched": 2022, "cursor": 2022,
}

# Prose tells. A real affiliation string is a short comma-delimited address
# ("Groq, Inc, Palo Alto, CA, USA"); a biography is a sentence. If the segment
# carrying the match reads like a sentence, the match is a word in prose rather
# than an employer.
PROSE_TELLS = (" is ", " was ", " received ", " graduated ", " served ",
               " joined ", " prior to ", " degree ", " his ", " her ", " they ")


def plausible_affiliation(raw: str, pattern: str) -> bool:
    """Does the matched text look like an employer, or like prose using the word?"""
    rx = re.compile(pattern, re.I)
    for segment in re.split(r"[;\n]|\s{2,}", raw):
        if not rx.search(segment):
            continue
        low = segment.lower()
        if any(t in low for t in PROSE_TELLS):
            continue
        # Real affiliation lines are short. A 200-character match is a paragraph.
        if len(segment) > 140:
            continue
        return True
    return False


def seed_company(conn: sqlite3.Connection, client: Client, company: str,
                 run_id: str) -> tuple[int, dict]:
    """Entry points into a company: people who named it as their own affiliation.

    Company-seeded rather than person-seeded. The org becomes a node and every
    person gets a works_at edge sourced to the paper carrying the string, so the
    claim "works at X" has a citation rather than being an inference from a
    LinkedIn-shaped guess.
    """
    key_c = company.strip().lower()
    pattern = COMPANY_PATTERNS.get(key_c, re.escape(company.strip()))
    founded = FOUNDED.get(key_c)
    raw_all = client.affiliated_people(company, pattern)

    # Two cheap guards before anything becomes a node, because a company name
    # that is also an ordinary English word produces confident-looking garbage.
    # Etched returned seven "employees", the oldest from 1991, all of them the
    # verb in a semiconductor abstract. Cursor returned an unrelated Chilean
    # "Cursor Ltd." and Spanish ecology prose.
    exclude = EXCLUDE_PATTERNS.get(key_c)
    exclude_rx = re.compile(exclude, re.I) if exclude else None
    people_raw, rejected = {}, 0
    for aid, rec in raw_all.items():
        if exclude_rx and exclude_rx.search(rec["raw"]):
            rejected += 1
            continue
        if founded and (rec["latest"] or 0) < founded:
            rejected += 1
            continue
        if not plausible_affiliation(rec["raw"], pattern):
            rejected += 1
            continue
        people_raw[aid] = rec

    # OpenAlex fragments one author across several ids -- Tri Dao has four, and
    # the raw result was 83 ids for 39 people. Two identical names both claiming
    # the same employer are the same person with near-certainty, so they are
    # merged here. Without this the queue gets the same researcher four times,
    # and the yield number is inflated by a factor of two.
    people: dict[str, dict] = {}
    for aid, rec in people_raw.items():
        key = re.sub(r"\s+", " ", rec["name"].strip().lower())
        cur = people.get(key)
        if cur is None:
            people[key] = {**rec, "openalex_ids": [aid]}
            continue
        cur["openalex_ids"].append(aid)
        cur["papers"] += rec["papers"]
        if rec["latest"] >= cur["latest"]:
            cur.update(latest=rec["latest"], work_url=rec["work_url"],
                       work_title=rec["work_title"], raw=rec["raw"])

    if rejected:
        G.log_expansion(conn, run_id, None, "filter",
                      f"dropped {rejected} affiliation match(es) for {company!r}: "
                      f"before founding year {founded} or prose rather than an "
                      f"affiliation line")

    oid = G.upsert_node(conn, "organization", company, run_id=run_id)

    for key, rec in people.items():
        ids = rec["openalex_ids"]
        nid = G.upsert_node(conn, "person", rec["name"],
                            external={"openalex": ids[0],
                                      "openalex_alt": ids[1:] or None},
                            attrs={"paper_institution": company,
                                   "papers_with_company": rec["papers"],
                                   "openalex_id_count": len(ids),
                                   "latest_year": rec["latest"]},
                            run_id=run_id)
        G.add_edge(conn, nid, oid, "works_at", source_url=rec["work_url"],
                   year=rec["latest"], is_current=True,
                   quote=f"own affiliation on \"{rec['work_title']}\" "
                         f"({rec['latest']}): {rec['raw']}")
        G.record_path(conn, nid, run_id, seed_node_id=oid, hops=1, via="works_at")
    return oid, people


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
                            attrs={
                                # From the shared paper's authorship, which is
                                # accurate but sparse. Not an assertion about
                                # where they work now -- that is what
                                # resolve_affiliation is for, and it writes
                                # affiliation_confidence alongside its answer.
                                "paper_institution": inst,
                                "topics": info["topics"],
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
        exp.added_people += 1
    return exp


# ------------------------------------------------------ affiliation


def resolve_affiliation(conn: sqlite3.Connection, client: Client, node_id: int,
                        author, run_id: str) -> tuple[str | None, float, str]:
    """Place a person at an organization, with a confidence and a reason.

    Ranked on sustained recency from OpenAlex `affiliations`, never on
    `last_known_institutions[0]`. Anything below 0.5 is stored as evidence with
    its doubt attached rather than asserted as fact.
    """
    from .openalex import current_affiliation

    inst, conf, why = current_affiliation(author)
    if not inst:
        return None, 0.0, why
    oid = G.upsert_node(conn, "organization", inst, run_id=run_id)
    G.add_edge(conn, node_id, oid, "works_at", source_url=author.url, is_current=True,
               quote=f"OpenAlex affiliations, ranked on sustained recency: {why}")
    attrs = json.loads(conn.execute("SELECT attrs FROM graph_nodes WHERE id=?",
                                    (node_id,)).fetchone()["attrs"] or "{}")
    attrs.update({"institution": inst, "affiliation_confidence": conf,
                  "affiliation_reason": why,
                  "conflation_risk": author.works_count > 150})
    conn.execute("UPDATE graph_nodes SET attrs = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(attrs), utcnow(), node_id))
    return inst, conf, why


def rank(conn: sqlite3.Connection, run_id: str, *, topic_terms: list[str],
         limit: int = 20) -> list[dict]:
    """Rank candidates before spending an affiliation lookup on any of them.

    Honest about its own weakness: without an affiliation, seniority band and
    company fit are unknown, so topic and recency carry nearly all the weight and
    path_count is the only structural signal available.
    """
    rows = conn.execute("""
        SELECT n.id, n.display_name, n.attrs, MIN(p.hops) hops
          FROM graph_paths p JOIN graph_nodes n ON n.id = p.node_id
         WHERE p.run_id IN (SELECT run_id FROM graph_paths)
           AND n.kind = 'person'
         GROUP BY n.id""").fetchall()
    out = []
    for r in rows:
        a = json.loads(r["attrs"] or "{}")
        if a.get("seed"):
            continue
        topics = " ".join(a.get("topics") or []).lower()
        hit = sum(1 for t in topic_terms if t.lower() in topics)
        score = G.score_node(
            conn, r["id"], hops=r["hops"] or 1,
            topic_match=min(1.0, hit / max(1, len(topic_terms))) if topics else 0.4,
            latest_year=a.get("latest_year"),
            email_resolvable=False,
            seniority=a.get("seniority", "unknown"))
        out.append({"id": r["id"], "name": r["display_name"], "hops": r["hops"],
                    "paths": G.path_count(conn, r["id"]), "score": score,
                    "attrs": a})
    out.sort(key=lambda x: (-x["paths"], -x["score"].total))
    return out[:limit]


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
