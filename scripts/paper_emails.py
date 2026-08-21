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
import time
import urllib.error
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import meter


def _contact() -> str:
    """The mailto these APIs ask for, from config rather than baked in."""
    try:
        from pathlib import Path as _P

        from .config import Config
        addr = Config(_P(__file__).resolve().parent.parent / "config").campaign.contact_email
        return f"mailto:{addr}" if addr else "no contact configured"
    except Exception:
        return "no contact configured"

UA = {"User-Agent": f"outbound-sourcing/1.0 ({_contact()})"}
ARXIV = "http://export.arxiv.org/api/query"
SEARCH_DELAY = 3.0      # arXiv's requested rate

PLAIN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BRACE = re.compile(r"[\{\[]([A-Za-z0-9._%+,\s-]+)[\}\]]\s*@\s*([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


# How old a paper may be before its addresses stop meaning current employment.
# A paper address proves where someone worked when it was submitted, not where
# they work now -- the same defect as a commit email, and it bit immediately:
# the 2022 Groq TSP paper yielded eight @groq.com addresses, and at least two of
# those authors (Dennis Abts, Sahil Parmar) have since moved to NVIDIA. Pitching
# them "your team at Groq" would be wrong about the one fact the email asserts.
MAX_PAPER_AGE_YEARS = 2


def paper_year_month(arxiv_id: str) -> tuple[int, int] | None:
    """arXiv ids encode YYMM, so age is free -- no extra request."""
    m = re.match(r"(\d{2})(\d{2})\.", arxiv_id)
    if not m:
        return None
    return 2000 + int(m.group(1)), int(m.group(2))


@dataclass
class PaperHit:
    arxiv_id: str
    title: str
    emails: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)


def search(query: str, max_results: int = 12) -> list[tuple[str, str, list[str]]]:
    """arXiv search. Returns (id, title, authors).

    arXiv asks for roughly three seconds between requests and answers 429 when
    that is ignored. A sweep across twenty companies is dozens of calls, and
    without backoff it died on the eighth -- the same failure the OpenAlex
    client already had fixed, in a module written after it.
    """
    url = (f"{ARXIV}?search_query={urllib.parse.quote(query)}"
           f"&max_results={max_results}&sortBy=submittedDate&sortOrder=descending")
    xml = ""
    backoff = 4.0
    for attempt in range(5):
        meter.bump("arxiv_calls")
        try:
            xml = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=40
            ).read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 4:
                meter.bump("arxiv_throttled")
                time.sleep(backoff)
                backoff *= 2
                continue
            return []
        except Exception:
            return []
    time.sleep(SEARCH_DELAY)
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
        meter.bump("pdf_failures")
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
            max_papers: int = 12, now_year: int = 2026,
            max_age_years: int | None = None) -> tuple[list[PaperHit], list[str]]:
    """Papers naming the company, and the addresses on their first pages.

    Returns (hits, skipped) where skipped names the papers dropped as too old to
    testify to current employment.
    """
    max_age = MAX_PAPER_AGE_YEARS if max_age_years is None else max_age_years
    queries = [f'all:"{company}"'] + [f'all:"{t}"' for t in (extra_terms or [])]
    seen_ids: set[str] = set()
    hits: list[PaperHit] = []
    skipped: list[str] = []
    for q in queries:
        for aid, title, authors in search(q, max_results=max_papers):
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            ym = paper_year_month(aid)
            if ym and (now_year - ym[0]) > max_age:
                skipped.append(f"{aid} ({ym[0]}-{ym[1]:02d}, {now_year - ym[0]}y old)")
                continue
            text = first_page_text(aid)
            if not text:
                continue
            emails = emails_in(text, domain)
            if emails:
                hits.append(PaperHit(aid, title, emails, authors))
    return hits, skipped


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
