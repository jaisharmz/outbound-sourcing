"""OpenAlex: coauthorship edges, and affiliations if you read the right field.

Measured against a real traversal, twice, because the first measurement was
wrong and the correction matters more than the original finding:

  coauthorship edges      reliable. Who wrote what with whom and when comes from
                          the paper itself and is the graph's backbone.
  last_known_institutions a LIST, and its first element is not the most recent.
                          Reading [0] returns "Berkeley College" for someone at
                          Together AI and "BioQ Pharma" for someone at Stanford.
                          Do not use it.
  affiliations[]          institution plus the years it was seen. Ranked on
                          sustained recency this resolves 6-7 of 8 spot checks
                          correctly, and the failures come back with low
                          confidence rather than confidently wrong.
  per-paper authorship
  institution             accurate when present, present on 8 of 73 authorships,
                          absent on the recent preprints that matter.

The one real defect left is profile conflation: OpenAlex merges some distinct
people into a single author record. "Junxiong Wang" carries 198 works and
resolves to a plausible institution at high confidence while being at least two
people. Works count wildly out of line with career stage is the tell.

Deterministic. No judgment about who is worth expanding lives here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

API = "https://api.openalex.org"


@dataclass
class Author:
    openalex_id: str
    name: str
    orcid: str | None = None
    works_count: int = 0
    cited_by_count: int = 0
    institutions: list[str] = field(default_factory=list)
    last_known_institution: str | None = None
    # (institution, years) straight from OpenAlex, unranked.
    affiliations: list[tuple[str, list[int]]] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    homepage: str | None = None

    @property
    def short_id(self) -> str:
        return self.openalex_id.rsplit("/", 1)[-1]

    @property
    def url(self) -> str:
        return f"https://openalex.org/{self.short_id}"


@dataclass
class Work:
    openalex_id: str
    title: str
    year: int | None
    authors: list[tuple[str, str, str | None]]   # (openalex_id, name, institution)
    doi: str | None = None
    topics: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://openalex.org/{self.openalex_id.rsplit('/', 1)[-1]}"


class OpenAlexError(RuntimeError):
    pass


@dataclass
class Client:
    # OpenAlex asks for a contact address and gives the polite pool in return,
    # which is both faster and the courteous thing to do.
    mailto: str | None = None
    calls: int = 0
    delay: float = 0.12

    def get(self, path: str, **params) -> dict:
        if self.mailto:
            params["mailto"] = self.mailto
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "outbound-sourcing/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.calls += 1
                time.sleep(self.delay)
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise OpenAlexError("rate limited by OpenAlex; slow down or set mailto")
            raise OpenAlexError(f"HTTP {exc.code} for {path}") from exc
        except Exception as exc:
            raise OpenAlexError(f"{type(exc).__name__} for {path}") from exc

    # ------------------------------------------------------------- authors

    def find_author(self, name: str, *, affiliation_hint: str | None = None) -> Author | None:
        data = self.get("/authors", search=name, per_page=10)
        results = data.get("results") or []
        if not results:
            return None
        if affiliation_hint:
            hint = affiliation_hint.lower()
            for r in results:
                inst = " ".join(
                    (i.get("display_name") or "") for i in (r.get("affiliations") or [])
                ).lower()
                last = ((r.get("last_known_institutions") or [{}])[0].get("display_name") or "")
                if hint in inst or hint in last.lower():
                    return self._author(r)
        return self._author(results[0])

    def author(self, openalex_id: str) -> Author:
        return self._author(self.get(f"/authors/{openalex_id.rsplit('/', 1)[-1]}"))

    @staticmethod
    def _author(r: dict) -> Author:
        affs: list[tuple[str, list[int]]] = []
        for a in r.get("affiliations") or []:
            name = (a.get("institution") or {}).get("display_name")
            if name:
                affs.append((name, sorted(a.get("years") or [], reverse=True)))
        insts = [n for n, _y in affs]
        # last_known_institutions is a LIST and its first element is not the
        # most recent. Reading [0] is what made this field look like bad data.
        last = (r.get("last_known_institutions") or [{}])
        return Author(
            affiliations=affs,
            openalex_id=r.get("id", ""),
            name=r.get("display_name", ""),
            orcid=r.get("orcid"),
            works_count=r.get("works_count", 0),
            cited_by_count=r.get("cited_by_count", 0),
            institutions=insts,
            last_known_institution=(last[0].get("display_name") if last else None),
            topics=[t.get("display_name") for t in (r.get("topics") or [])[:6]
                    if t.get("display_name")],
            homepage=(r.get("ids") or {}).get("orcid"),
        )

    # -------------------------------------------------------- affiliation

    # --------------------------------------------------------------- works

    def works(self, author_id: str, *, since: int | None = None,
              per_page: int = 50) -> list[Work]:
        params = {"filter": f"author.id:{author_id.rsplit('/', 1)[-1]}",
                  "per_page": per_page, "sort": "publication_year:desc"}
        if since:
            params["filter"] += f",from_publication_date:{since}-01-01"
        data = self.get("/works", **params)
        out = []
        for w in data.get("results") or []:
            authors = []
            for a in w.get("authorships") or []:
                au = a.get("author") or {}
                inst = (a.get("institutions") or [{}])
                authors.append((au.get("id") or "", au.get("display_name") or "",
                                inst[0].get("display_name") if inst else None))
            out.append(Work(
                openalex_id=w.get("id", ""), title=w.get("title") or "",
                year=w.get("publication_year"), authors=authors, doi=w.get("doi"),
                topics=[t.get("display_name") for t in (w.get("topics") or [])[:4]
                        if t.get("display_name")],
            ))
        return out


def coauthor_counts(works: list[Work], exclude_id: str) -> dict[str, dict]:
    """Aggregate coauthors across works: how often, how recently, where."""
    out: dict[str, dict] = {}
    ex = exclude_id.rsplit("/", 1)[-1]
    for w in works:
        for aid, name, inst in w.authors:
            # OpenAlex leaves author ids null on some records; a name with no id
            # cannot be deduplicated, so it is not a graph node.
            short = (aid or "").rsplit("/", 1)[-1]
            if not short or short == ex or not name:
                continue
            e = out.setdefault(short, {"name": name, "count": 0, "latest": 0,
                                       "institutions": set(), "papers": [],
                                       "topics": set()})
            e["count"] += 1
            e["latest"] = max(e["latest"], w.year or 0)
            if inst:
                e["institutions"].add(inst)
            # Topics come from the shared paper, which is free and already
            # fetched. Without this a coauthor node has no topical signal at all
            # and every candidate scores identically.
            e["topics"].update(w.topics)
            if len(e["papers"]) < 3:
                e["papers"].append((w.title, w.year, w.url))
    for e in out.values():
        e["institutions"] = sorted(e["institutions"])
        e["topics"] = sorted(e["topics"])
    return out


def current_affiliation(author: Author, *, now: int = 2026,
                        window: int = 3) -> tuple[str | None, float, str]:
    """Best guess at where someone works now, with a confidence and a reason.

    Ranked on sustained recency rather than on OpenAlex's own ordering. A single
    recent year is usually a parsing artefact -- Ben Athiwaratkun shows "Berkeley
    College" for 2026 alone next to "Together" for 2026, 2025 and 2024, and the
    sustained one is the true employer. Reading last_known_institutions[0] picks
    the artefact.
    """
    if not author.affiliations:
        return None, 0.0, "no affiliation data"
    scored = []
    for name, years in author.affiliations:
        if not years:
            continue
        recent = [y for y in years if y >= now - window]
        if not recent:
            continue
        # sustained recent presence beats a single-year blip
        scored.append((len(recent) + max(recent) / 10000, name, years, len(recent)))
    if not scored:
        newest = max((max(y) for _n, y in author.affiliations if y), default=None)
        return None, 0.0, f"no affiliation inside the last {window} years (newest {newest})"
    scored.sort(reverse=True)
    _s, name, years, n_recent = scored[0]
    rivals = [x for x in scored[1:] if x[3] >= n_recent]
    conf = 0.9 if n_recent >= 2 and not rivals else (0.6 if not rivals else 0.4)
    reason = f"{name} in {', '.join(str(y) for y in years[:4])}"
    if rivals:
        reason += f"; contested by {rivals[0][1]}"
    return name, conf, reason
