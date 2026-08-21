"""Generate a research brief for one company.

The brief is assembled at runtime from `config/icp.yaml`, `config/dorks.yaml`,
the account record and anything already known about its people. Nothing about a
particular user or target is written here -- that is Rule 2, and it is why this
module reads config rather than containing copy.

    python -m scripts.research_brief --company "Kaedim"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import Config, load_config
from .db import open_db
from .errors import ConfigError


def render_dorks(config: Config, company: str) -> list[str]:
    out = []
    for d in config.dorks:
        if not d.enabled:
            continue
        out.append(f"  {d.id:<24} {d.query.replace('{company}', company)}   [{d.signal}]")
    return out


def known_people(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, role, provenance, source_url FROM known_people"
        " WHERE account_id = ? AND resolved = 0 ORDER BY name", (account_id,)
    ).fetchall()


def build(config: Config, conn: sqlite3.Connection, account: sqlite3.Row) -> str:
    icp = config.icp
    budget = config.campaign.discovery.subagent_tool_budget
    people = known_people(conn, account["id"])
    slug = account["name_normalized"].replace(" ", "-")

    lines: list[str] = []
    add = lines.append

    add(f"# Research brief: {account['name']}")
    add("")
    add(f"Domain: {account['domain'] or '(unresolved -- find it first)'}")
    if account["what"]:
        add(f"Investor blurb: {account['what']}")
    if account["homepage_text"]:
        add(f"Their own words: {account['homepage_text'][:400]}")
    if account["homepage_fetch_status"] and account["homepage_fetch_status"] != "ok":
        add(f"NOTE: their homepage came back `{account['homepage_fetch_status']}`, so we have "
            f"little of their own copy. Expect to work harder for grounding, and do not "
            f"mistake our failure to fetch for the company being small.")
    if account["entry_note"]:
        add(f"Route in, per an earlier run: {account['entry_note']}")
    if account["prefilter_evidence"]:
        add(f"Why this company is in scope: {account['prefilter_evidence']}")
    add("")

    add(f"## Budget: {budget} tool calls")
    add("")
    add("Track them. If you run out, say so: set `budget_exhausted: true` and report")
    add("`searches_used`. A thin answer labelled thin is useful. A thin answer that looks")
    add("complete is worse than nothing, because nothing downstream can tell the difference.")
    add("")

    if people:
        add("## People already known -- do not spend budget rediscovering these")
        add("")
        for p in people:
            add(f"  {p['name']} ({p['role']}) -- from {p['provenance']}, {p['source_url'] or 'no url'}")
        add("")
        add("For these, the job is to find or infer an email and ground it. Confirm the")
        add("person is still there before writing them down; a portfolio page is not a")
        add("current-affiliation check.")
        add("")

    add("## Who counts")
    add("")
    add(f"Titles: {', '.join(icp.titles)}")
    if icp.title_excludes:
        add(f"Exclude: {', '.join(icp.title_excludes)}")
    add(f"At most {icp.max_contacts_per_company} people.")
    if icp.exclude_regions:
        add(f"Skip anyone whose country is in {', '.join(icp.exclude_regions)}.")
    add(f"Minimum confidence to be worth emitting: {icp.min_confidence}")
    add("")

    add("## Search seeds -- starting points, improvise beyond them")
    add("")
    lines.extend(render_dorks(config, account["name"]))
    add("")

    add("## Sources that work")
    add("")
    add("These give a name and a real email in the same document, which is what makes")
    add("pattern inference possible: arXiv PDFs (author emails in the header), Semantic")
    add("Scholar / OpenAlex, GitHub public commit emails, personal academic sites and CVs,")
    add("and the company's own /team, /research, /about, /people pages.")
    add("")
    add("**LinkedIn: search-result snippets only.** Read names and titles off the SERP. Do")
    add("not fetch, crawl or automate linkedin.com. Resolve names found there to emails")
    add("through the other channels.")
    add("")

    add("## Two checks that have each cost us a real error")
    add("")
    add("**Never conclude a page lacks data from a converted fetch.** WebFetch renders to")
    add("markdown first, and whatever the conversion drops is invisible to you and looks")
    add("exactly like absence. A team page that renders to three names when the company")
    add("clearly has thirty is a signal to look at the raw HTML, not a finding. `curl` it")
    add("and grep for `data-` attributes and framework JSON payloads.")
    add("")
    add("**Confirm you have the right company.** A search on a common name returns a")
    add("different company with that name often enough to matter -- three of sixteen in one")
    add("hand-checked sample. Check the result against this company's domain before")
    add("believing it.")
    add("")

    add("## Evidence")
    add("")
    add("Every record needs one evidence item grounding the name/title/company binding and")
    add("one grounding the email, each with an absolute URL and a real quote. A")
    add("personalization line needs its own source URL and must be one or two complete")
    add("sentences -- it is dropped into the email as its own paragraph and the template")
    add("cannot fix grammar.")
    add("")
    add("If you cannot ground a detail about someone's work, set `personalization: null`.")
    add("That is the right answer, not a failure.")
    add("")

    add(f"## Write `state/candidates/{slug}.json`")
    add("")
    add("Against this schema, then stop:")
    add("")
    add("```json")
    add(json.dumps(_schema_summary(), indent=2))
    add("```")
    add("")
    add("If you find nobody, write the file with an empty `candidates` list and a `reason`")
    add("saying what you looked at. Never pad.")
    return "\n".join(lines)


def _schema_summary() -> dict:
    return {
        "company": "<name>",
        "domain": "<domain or null>",
        "generated_at": "<ISO8601>",
        "searches_used": 0,
        "tool_calls_used": 0,
        "budget_exhausted": False,
        "reason": "<required only when candidates is empty>",
        "candidates": [{
            "name": "...", "title": "...", "company": "...", "email": "...",
            "email_basis": "observed | inferred_from_pattern",
            "evidence": [{"claim": "...", "url": "https://...", "quote": "...",
                          "retrieved_at": "<ISO8601>"}],
            "personalization": "<one or two complete sentences, or null>",
            "personalization_source_url": "https://... or null",
            "confidence": 0.0,
            "country": "US",
        }],
    }


def select(conn: sqlite3.Connection, *, campaign: str | None = None,
           prefilter: str = "pass_builds", limit: int = 10,
           status: tuple[str, ...] = ("new", "degraded")) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(status))
    return conn.execute(
        f"SELECT * FROM accounts WHERE prefilter = ? AND status IN ({marks})"
        + (" AND campaign = ?" if campaign else "")
        + " ORDER BY (domain IS NULL), name LIMIT ?",
        (prefilter, *status, *([campaign] if campaign else []), limit),
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a research brief.")
    ap.add_argument("--company")
    ap.add_argument("--campaign")
    ap.add_argument("--prefilter", default="pass_builds")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--list", action="store_true", help="list candidates for research")
    ap.add_argument("--config")
    ap.add_argument("--db")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    conn = open_db(args.db)

    if args.company:
        row = conn.execute("SELECT * FROM accounts WHERE name = ?", (args.company,)).fetchone()
        if not row:
            print(f"no account named {args.company!r}", file=sys.stderr)
            return 2
        rows = [row]
    else:
        rows = select(conn, campaign=args.campaign, prefilter=args.prefilter, limit=args.limit)

    if args.list:
        for r in rows:
            n = conn.execute("SELECT COUNT(*) FROM known_people WHERE account_id = ?",
                             (r["id"],)).fetchone()[0]
            print(f"  {r['name']:<26} {str(r['domain']):<26} "
                  f"homepage={r['homepage_fetch_status']:<9} known_people={n}")
        return 0

    for r in rows:
        print(build(config, conn, r))
        print("\n" + "=" * 78 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
