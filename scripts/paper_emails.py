"""Read author emails off the first page of a paper.

The channel that works for the population personal pages miss. Together AI's
researchers keep github.io sites with contact details; Groq's do not -- 48 of 72
had no page at all, and the two addresses found were a role account and a gmail.
Systems and hardware people publish at ISCA/MICRO/ASPLOS rather than blogging,
and those papers put author emails under the title, at the company domain,
because that is the venue convention.

So the address comes from the document the person wrote, which is the strongest
evidence available: first-party, attributable, and at the employer being pitched.

Two extraction details that matter:

  The brace form. Academic first pages compress shared domains as
  `{alice,bob,carol}@company.com`. A reader that only understands plain
  addresses finds nothing on exactly the papers most likely to list a whole
  team.

  Page one only. Later pages carry references, and a bibliography is full of
  other people's addresses. Reading the whole PDF would attribute a cited
  author's email to the paper's author.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import meter

UA = {"User-Agent": "outbound-sourcing/1.0 (mailto:jaisharmaus@gmail.com)"}
ARXIV = "http://export.arxiv.org/api/query"

PLAIN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BRACE = re.compile(r"[\{\[]([A-Za-z0-9._%+,\s-]+)[\}\]]\s*@\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


@dataclass
class PaperHit:
    arxiv_id: str
    title: str
    emails: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)


def search(query: str, max_results: int = 12) -> list[tuple[str, str, list[str]]]:
    """arXiv search. Returns (id, title, authors)."""
    url = (f"{ARXIV}?search_query={urllib.parse.quote(query)}"
           f"&max_results={max_results}&sortBy=submittedDate&sortOrder=descending")
    meter.bump("arxiv_calls")
    xml = urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8", "replace")
    entries = xml.split("<entry>")[1:]
    out = []
    for e in entries:
        aid = re.search(r"<id>http://arxiv\.org/abs/([^<]+)</id>", e)
        title = re.search(r"<title>([^<]+)</title>", e)
        if not aid:
            continue
        authors = [re.sub(r"\s+", " ", a).strip()
                   for a in re.findall(r"<name>([^<]+)</name>", e)]
        out.append((aid.group(1), re.sub(r"\s+", " ", (title.group(1) if title else "")).strip(),
                    authors))
    return out


def first_page_text(arxiv_id: str) -> str:
    meter.bump("pdf_fetches")
    try:
        data = urllib.request.urlopen(
            urllib.request.Request(f"https://arxiv.org/pdf/{arxiv_id}", headers=UA),
            timeout=60).read()
    except Exception:
        return ""
    with tempfile.TemporaryDirectory() as d:
        pdf, txt = Path(d) / "p.pdf", Path(d) / "p.txt"
        pdf.write_bytes(data)
        try:
            subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(pdf), str(txt)],
                           check=False, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return ""
        return txt.read_text(errors="replace") if txt.exists() else ""


def emails_in(text: str, domain: str | None = None) -> list[str]:
    """Every address on the page, brace form expanded, optionally filtered."""
    found: list[str] = []
    for names, dom in BRACE.findall(text):
        found += [f"{n.strip()}@{dom}" for n in re.split(r"[,\s]+", names) if n.strip()]
    # Strip the brace groups before the plain pass, or "{a,b}@x.com" also yields
    # the bogus address "b}@x.com".
    found += PLAIN.findall(BRACE.sub(" ", text))
    out, seen = [], set()
    for e in found:
        e = e.strip().lower().rstrip(".,;")
        if e in seen or " " in e:
            continue
        if domain and not e.endswith("@" + domain) and not e.endswith("." + domain):
            continue
        seen.add(e)
        out.append(e)
    return out


def harvest(company: str, domain: str, extra_terms: list[str] | None = None,
            max_papers: int = 12) -> list[PaperHit]:
    """Papers naming the company, and the addresses on their first pages."""
    queries = [f'all:"{company}"'] + [f'all:"{t}"' for t in (extra_terms or [])]
    seen_ids: set[str] = set()
    hits: list[PaperHit] = []
    for q in queries:
        for aid, title, authors in search(q, max_results=max_papers):
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            text = first_page_text(aid)
            if not text:
                continue
            emails = emails_in(text, domain)
            if emails:
                hits.append(PaperHit(aid, title, emails, authors))
    return hits


def pair(hit: PaperHit) -> tuple[dict[str, str], dict[str, int]]:
    """Attribute each address on the page to a named author of that page.

    The paper gives both lists but not the mapping, so the mapping is derived by
    testing the conventions institutions actually use. An address that matches no
    author is left unattributed rather than guessed onto the nearest name -- an
    address without an identity is the thing this channel exists to avoid.

    Returns (email -> author name, pattern -> count) so the convention the domain
    uses is measured rather than assumed, and can be applied to colleagues who
    were not on this paper.
    """
    def forms(name: str) -> dict[str, str]:
        parts = [p for p in re.split(r"[^A-Za-z]+", name.lower()) if p]
        if len(parts) < 2:
            return {}
        f, l = parts[0], parts[-1]
        return {
            "first_initial_last": f"{f[0]}{l}",
            "first_last": f"{f}{l}",
            "first.last": f"{f}.{l}",
            "first": f,
            "initials": f"{f[0]}{l[0]}",
            "last": l,
        }

    attributed: dict[str, str] = {}
    counts: dict[str, int] = {}
    claimed: set[str] = set()
    # Most specific conventions first: "first" and "initials" collide easily, so
    # a distinctive match must win before a two-letter one can claim the address.
    for style in ("first_initial_last", "first_last", "first.last", "last",
                  "first", "initials"):
        for email in hit.emails:
            if email in attributed:
                continue
            local = email.partition("@")[0]
            for author in hit.authors:
                if author in claimed and style in ("first", "initials"):
                    continue
                if forms(author).get(style) == local:
                    attributed[email] = author
                    claimed.add(author)
                    counts[style] = counts.get(style, 0) + 1
                    break
    return attributed, counts
