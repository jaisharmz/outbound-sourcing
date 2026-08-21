"""Industry mode: propose companies, let the operator pick, research only those.

Deciding which companies belong to "AI inference" is judgment and stays with the
model. Everything here is mechanism: pulling candidates already on hand, laying
them out for a decision, and parsing the answer.

Two rules the display exists to enforce:

**Descriptions come from the company's own words.** Investor blurbs misjudge
companies badly -- Kaedim's read "game-ready on-demand 3D assets" while its
homepage opens "AI-powered 3D asset creation". Where only a blurb exists, it is
labelled as the investor's.

**Funding is marked unknown rather than guessed.** Several fund sources yield a
name and nothing else; inventing a stage from a search of unknown quality is
worse than an empty field.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

DEFAULT_LIMIT = 15


@dataclass
class Suggestion:
    name: str
    domain: str | None = None
    description: str | None = None
    description_source: str = "none"      # homepage | investor | none
    stage: str | None = None
    raised: str | None = None
    investors: str | None = None
    fund: str | None = None
    relationship: str | None = None       # e.g. fellowship -> warm intro
    account_id: int | None = None

    @property
    def funding_line(self) -> str:
        if not self.stage and not self.raised:
            return "Funding unknown"
        bits = [b for b in (self.stage, self.raised) if b]
        return "Stage: " + ", ".join(bits)


def from_accounts(conn: sqlite3.Connection, terms: list[str],
                  limit: int = 60) -> list[Suggestion]:
    """Companies already on hand whose own words match the industry terms."""
    if not terms:
        return []
    clauses, params = [], []
    for t in terms:
        clauses.append("(LOWER(COALESCE(a.homepage_text,'') || ' ' || COALESCE(a.what,'')"
                       " || ' ' || a.name) LIKE ?)")
        params.append(f"%{t.lower()}%")
    rows = conn.execute(f"""
        SELECT a.id, a.name, a.domain, a.homepage_text, a.what, a.stages, a.verticals,
               a.fund, a.relationship, a.homepage_fetch_status
          FROM accounts a
         WHERE a.status NOT IN ('excluded','excluded_region','merged')
           AND a.validation_run = 0
           AND ({' OR '.join(clauses)})
         ORDER BY (a.homepage_text IS NULL), a.name
         LIMIT ?""", (*params, limit)).fetchall()

    out = []
    for r in rows:
        if r["homepage_text"] and r["homepage_fetch_status"] == "ok":
            desc, src = _first_sentences(r["homepage_text"]), "homepage"
        elif r["what"]:
            desc, src = _first_sentences(r["what"]), "investor"
        else:
            desc, src = None, "none"
        out.append(Suggestion(
            name=r["name"], domain=r["domain"], description=desc, description_source=src,
            stage=r["stages"], fund=r["fund"], relationship=r["relationship"],
            account_id=r["id"],
        ))
    return out


def _first_sentences(text: str, limit: int = 150) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > 60 else cut.rstrip() + "…")


def render(items: list[Suggestion], topic: str, *, limit: int = DEFAULT_LIMIT,
           offset: int = 0) -> str:
    """The numbered list the operator picks from."""
    window = items[offset:offset + limit]
    lines = [topic, ""]
    for i, s in enumerate(window, start=offset + 1):
        lines.append(f"{i:>2}. {s.name}" + (f" — {s.description}" if s.description else ""))
        meta = [s.funding_line]
        if s.investors:
            meta.append(s.investors)
        if s.fund:
            meta.append(f"via {s.fund}")
        if s.description_source == "investor":
            meta.append("description is the investor's, not the company's")
        elif s.description_source == "none":
            meta.append("no description available")
        lines.append("    " + " · ".join(meta))
        if s.relationship:
            lines.append(f"    ⚑ warm route available ({s.relationship}) — an intro beats a cold email here")
        lines.append("")
    shown = offset + len(window)
    if shown < len(items):
        lines.append(f"[{shown} of {len(items)} shown — say 'more' for the next {limit}]")
    lines.append("")
    lines.append("Pick by number (3), range (1-5), list (1,4,7), or 'all'.")
    return "\n".join(lines)


class SelectionError(ValueError):
    pass


def parse_selection(text: str, count: int) -> list[int]:
    """'1,4,7' | '1-5' | 'all' -> zero-based indices. Rejects out-of-range."""
    t = (text or "").strip().lower()
    if not t:
        raise SelectionError("no selection given")
    if t in ("all", "*"):
        return list(range(count))
    picked: set[int] = set()
    for part in re.split(r"[,\s]+", t):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise SelectionError(f"range {part!r} runs backwards")
            rng = range(lo, hi + 1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            raise SelectionError(f"{part!r} is not a number, a range, or 'all'")
        for n in rng:
            if not 1 <= n <= count:
                raise SelectionError(f"{n} is outside 1-{count}")
            picked.add(n - 1)
    if not picked:
        raise SelectionError("no selection given")
    return sorted(picked)
