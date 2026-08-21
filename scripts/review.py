"""The review gate: export contacts for approval, re-import the decisions.

This is now the only quality control between an inferred address and a
stranger's inbox, so the export is built around what could be wrong rather than
around what is convenient to print. Every row carries how its address was
arrived at and how much evidence that rests on.

    python -m scripts.review export --out review.md
    python -m scripts.review import --file reviewed.csv
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from .config import load_config
from .db import open_db, transaction, utcnow
from .errors import ConfigError
from .review_select import choose

COLUMNS = [
    "contact_id", "approved", "name", "title", "company", "email",
    "email_basis", "pattern", "pattern_samples", "pattern_confidence",
    "address_age", "verification", "confidence", "liveness",
    "personalization", "personalization_source",
]


def rows(conn: sqlite3.Connection, campaign: str | None = None) -> list[sqlite3.Row]:
    where = ["c.approved = 0", "c.sendable = 1", "a.validation_run = 0",
             "a.status NOT IN ('excluded','excluded_region','merged')"]
    params: list = []
    if campaign:
        where.append("c.campaign = ?")
        params.append(campaign)
    return conn.execute(f"""
        SELECT c.id, c.name, c.title, c.email, c.email_basis, c.confidence,
               c.verification_status, c.personalization, c.personalization_source_url,
               a.name AS company, a.email_pattern, a.email_pattern_samples,
               a.email_pattern_confidence, a.liveness_status, a.liveness_note,
               (SELECT k.email_observed_at FROM known_people k
                 WHERE k.account_id = a.id AND k.email = c.email) AS observed_at
          FROM contacts c JOIN accounts a ON a.id = c.account_id
         WHERE {' AND '.join(where)} ORDER BY a.name, c.name""", tuple(params)).fetchall()


def risk_flags(r: sqlite3.Row) -> list[str]:
    """What a reviewer should look at twice on this row."""
    out = []
    if r["email_basis"] == "inferred_from_pattern":
        n = r["email_pattern_samples"] or 0
        conf = r["email_pattern_confidence"] or 0
        out.append(f"address INFERRED from {n} sample(s) at {conf:.0%} agreement")
        if n <= 1:
            out.append("single-sample pattern: one address taught us this convention")
        if conf < 0.7:
            out.append("the domain mixes conventions; inference here is close to a guess")
    if r["observed_at"]:
        from datetime import datetime, timezone
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00"))).days
            if age > 730:
                out.append(f"address last seen {age // 365} years ago; a commit proves it "
                           f"existed then, not that the person is still reachable")
        except ValueError:
            pass
    if r["verification_status"] == "catch_all":
        out.append("domain is accept-all, so verification could not confirm the mailbox")
    if r["verification_status"] == "mx_only":
        out.append("domain accepts mail but the mailbox was never probed: outbound port 25 "
                   "is blocked from this network, so SMTP verification cannot run at all")
    if r["verification_status"] in ("unverified", "unknown"):
        out.append(f"verification status is {r['verification_status']}; this row should not send")
    if not r["personalization"]:
        out.append("no personalization; the template falls back")
    if r["liveness_status"] and r["liveness_status"] != "live":
        out.append(f"company liveness: {r['liveness_status']}")
    return out


def export(conn, config, out_path: Path, campaign: str | None = None) -> tuple[int, int]:
    data = rows(conn, campaign)
    csv_path = out_path.with_suffix(".csv")
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in data:
            w.writerow([
                r["id"], "", r["name"], r["title"], r["company"], r["email"],
                r["email_basis"], r["email_pattern"], r["email_pattern_samples"],
                f"{r['email_pattern_confidence']:.2f}" if r["email_pattern_confidence"] else "",
                (r["observed_at"] or "")[:10], r["verification_status"], r["confidence"],
                r["liveness_status"] or "", r["personalization"] or "",
                r["personalization_source_url"] or "",
            ])

    lines = [f"# Review — {len(data)} contacts awaiting approval", ""]
    lines.append(f"Edit the `approved` column in `{csv_path.name}` and re-import. "
                 f"Only approved rows queue.")
    lines.append("")
    flagged = [r for r in data if risk_flags(r)]
    lines.append(f"**{len(flagged)} of {len(data)} rows carry a risk flag.** "
                 f"They are listed first.")
    lines.append("")
    for r in sorted(data, key=lambda x: -len(risk_flags(x))):
        lines.append(f"### {r['name']} — {r['title']} @ {r['company']}")
        lines.append("")
        lines.append(f"- **{r['email']}** ({r['email_basis']})")
        if r["email_pattern"]:
            lines.append(f"- pattern `{r['email_pattern']}`, "
                         f"{r['email_pattern_samples']} samples, "
                         f"{r['email_pattern_confidence']:.0%} agreement")
        if r["observed_at"]:
            lines.append(f"- address observed in a commit dated {r['observed_at'][:10]}")
        if r["liveness_note"]:
            lines.append(f"- liveness: {r['liveness_note']}")
        lines.append(f"- personalization: "
                     + (f"\"{r['personalization']}\" ({r['personalization_source_url']})"
                        if r["personalization"] else "_none — template falls back_"))
        for f in risk_flags(r):
            lines.append(f"- ⚠ {f}")
        ev = conn.execute("SELECT claim, url, quote FROM evidence WHERE contact_id = ?",
                          (r["id"],)).fetchall()
        for e in ev:
            lines.append(f"- evidence — {e['claim']}")
            lines.append(f"    - {e['url']}")
            lines.append(f"    - \"{e['quote'][:160]}\"")
        lines.append("")

    picks = choose(conn, campaign=campaign)
    lines.append("---")
    lines.append("")
    lines.append("## Five rendered emails, chosen to span the variants")
    lines.append("")
    from . import templates as tpl
    step = config.steps_for(campaign or "startup")[0]
    for p in picks:
        row = conn.execute("""SELECT c.*, a.name AS account_name, a.domain AS account_domain
                              FROM contacts c JOIN accounts a ON a.id=c.account_id
                              WHERE c.id = ?""", (p.contact_id,)).fetchone()
        if not row:
            continue
        from .normalize import display_company
        contact = {"first_name": row["first_name"], "last_name": row["last_name"],
                   "name": row["name"], "title": row["title"], "email": row["email"],
                   "personalization": row["personalization"]}
        account = {"name": display_company(row["account_name"]),
                   "domain": row["account_domain"]}
        mailboxes = config.mailboxes.enabled()
        mb = mailboxes[0] if mailboxes else None
        try:
            email = tpl.render(config, step, contact=contact, account=account,
                               to=row["email"], campaign=campaign or "startup",
                               from_header=mb.from_.header() if mb else "",
                               reply_to=mb.reply_to if mb else None)
        except ConfigError as exc:
            lines.append(f"### {row['name']} — render failed: {exc}")
            continue
        lines.append(f"### {row['name']} @ {row['account_name']}")
        lines.append(f"*chosen because: {p.reason}*")
        lines.append("")
        lines.append("```")
        lines.append(email.preview())
        lines.append("```")
        lines.append("")

    out_path.write_text("\n".join(lines))
    return len(data), len(flagged)


def import_decisions(conn, path: Path) -> dict[str, int]:
    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    with Path(path).open(newline="") as fh:
        for row in csv.DictReader(fh):
            cid = (row.get("contact_id") or "").strip()
            decision = (row.get("approved") or "").strip().lower()
            if not cid.isdigit():
                continue
            if decision in ("y", "yes", "1", "true", "approved"):
                conn.execute("UPDATE contacts SET approved = 1, approved_at = ?,"
                             " status = 'approved' WHERE id = ?", (utcnow(), int(cid)))
                counts["approved"] += 1
            elif decision in ("n", "no", "0", "false", "rejected"):
                conn.execute("UPDATE contacts SET approved = 0, status = 'dropped',"
                             " stopped_reason = 'rejected at review' WHERE id = ?", (int(cid),))
                counts["rejected"] += 1
            else:
                counts["skipped"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Human review gate.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("--out", default="review.md")
    e.add_argument("--campaign"); e.add_argument("--config"); e.add_argument("--db")
    i = sub.add_parser("import"); i.add_argument("--file", required=True)
    i.add_argument("--db")
    args = ap.parse_args(argv)

    conn = open_db(getattr(args, "db", None))
    if args.cmd == "export":
        config = load_config(args.config)
        n, flagged = export(conn, config, Path(args.out), args.campaign)
        print(f"exported {n} contacts ({flagged} flagged) to {args.out} and "
              f"{Path(args.out).with_suffix('.csv').name}")
        return 0
    with transaction(conn):
        counts = import_decisions(conn, Path(args.file))
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
