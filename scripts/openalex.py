"""OpenAlex: the source for coauthorship edges. NOT for affiliations.

Measured against a real traversal before being trusted, and the result split
sharply:

  coauthorship edges      reliable. Who wrote what with whom and when comes
                          from the paper itself and is the graph's backbone.
  author-level
  last_known_institution  UNRELIABLE. Six of ten checked were wrong, and wrong
                          in a way that reads as authoritative: Ben Athiwaratkun
                          (Together AI) came back "Berkeley College", Michael
                          Poli "BioQ Pharma", Ce Zhang "Ministry of Natural
                          Resources". Resolving by OpenAlex ID rather than by
                          name did not help, so it is bad data rather than a
                          name collision. Never write a works_at edge from it.
  per-paper authorship
  institution             accurate when present, present 11% of the time, and
                          absent on exactly the recent preprints that matter.

So the premise that OpenAlex supplies affiliations does not hold. It supplies
the graph; where someone works has to come from their own page, a company team
page, or a paper footnote.

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
        insts = [i.get("display_name") for i in (r.get("affiliations") or [])
                 if i.get("display_name")]
        last = (r.get("last_known_institutions") or [{}])
        return Author(
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
                                       "institutions": set(), "papers": []})
            e["count"] += 1
            e["latest"] = max(e["latest"], w.year or 0)
            if inst:
                e["institutions"].add(inst)
            if len(e["papers"]) < 3:
                e["papers"].append((w.title, w.year, w.url))
    for e in out.values():
        e["institutions"] = sorted(e["institutions"])
    return out
