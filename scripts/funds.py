"""Fund portfolio extraction.

Two funds, two genuinely different page structures, so this is a per-fund
strategy rather than one parser:

    embedded_json     the whole roster sits in a `data-` attribute on the page
                      (a16z: 855 entries, one request, 100% domain coverage)
    list_plus_detail  the index page links to per-company pages that carry the
                      real domain and the founders (Lux: 1 + N requests)

This is deterministic ingest, not a crawler: one declared URL per fund from
`config/funds.yaml`, cached to disk, no link-following beyond the detail pages a
strategy explicitly names. Judgment about *which* companies matter happens later,
in the pre-filter and in the research subagent.

Everything fetched is cached under state/cache/funds/, so re-running stage 0 with
better rules costs nothing.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

UA = "Mozilla/5.0 (compatible; outbound-sourcing/0.1)"
CACHE_TTL_SECONDS = 24 * 3600


class FundError(RuntimeError):
    """A fund page that could not be read or understood."""


def cache_dir() -> Path:
    p = Path(__file__).resolve().parent.parent / "state" / "cache" / "funds"
    p.mkdir(parents=True, exist_ok=True)
    return p


def fetch(url: str, *, cache_key: str, ttl: int = CACHE_TTL_SECONDS,
          force: bool = False) -> str:
    """Fetch a URL, cached to disk by key."""
    path = cache_dir() / f"{cache_key}.html"
    if not force and path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        return path.read_text(encoding="utf-8", errors="replace")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        raise FundError(f"could not fetch {url}: {exc}") from exc
    path.write_text(body, encoding="utf-8")
    return body


@dataclass
class PortfolioCompany:
    name: str
    domain_url: str | None = None
    description: str | None = None
    founders: list[str] = field(default_factory=list)
    stages: str | None = None
    verticals: str | None = None
    year_founded: str | None = None
    status: str | None = None
    detail_url: str | None = None
    fund: str = ""


# --------------------------------------------------------------- embedded_json


def find_embedded_json(page: str, attribute: str) -> list[dict[str, Any]]:
    """Pull a JSON array out of an HTML `data-` attribute.

    This is the case that markdown conversion hides completely: the page renders
    to a handful of stale entries while the full roster sits in the source. See
    the required check in references/discovery.md.
    """
    match = re.search(rf'{re.escape(attribute)}="(\[.*?\])"\s', page, re.S)
    if not match:
        raise FundError(
            f"no `{attribute}` attribute found. If the page looked thin, check the raw "
            f"HTML for other data- attributes before concluding it has no data."
        )
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError as exc:
        raise FundError(f"`{attribute}` is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise FundError(f"`{attribute}` is not a list")
    return [d for d in data if isinstance(d, dict)]


def split_founders(value: Any) -> list[str]:
    """`founders_list` is a comma-joined string despite its name."""
    if isinstance(value, list):
        parts = [str(v) for v in value]
    elif isinstance(value, str):
        parts = re.split(r",\s*|\s+&\s+|\s+and\s+", value)
    else:
        return []
    out = []
    for p in parts:
        p = p.strip(" .,;")
        # Guard against a role or a sentence landing in a name field.
        if p and 1 < len(p.split()) <= 4 and not p.lower().startswith(("ceo", "co-founder")):
            out.append(p)
    return out


def parse_embedded_json(page: str, spec: dict[str, Any], fund: str) -> list[PortfolioCompany]:
    rows = find_embedded_json(page, spec.get("attribute", "data-companies"))
    keep = spec.get("status_contains")
    out = []
    for r in rows:
        status = str(r.get(spec.get("status_field", "status")) or "")
        if keep and keep not in status:
            continue
        name = str(r.get(spec.get("name_field", "name")) or "").strip()
        if not name:
            continue
        verticals = r.get(spec.get("verticals_field", "focus_areas"))
        stages = r.get(spec.get("stages_field", "stages"))
        out.append(PortfolioCompany(
            name=name,
            domain_url=str(r.get(spec.get("url_field", "company_url")) or "") or None,
            description=str(r.get(spec.get("description_field", "website_description")) or "") or None,
            founders=split_founders(r.get(spec.get("founders_field", "founders_list"))),
            # Notes only. a16z lists Cursor's stage as M&A, which is wrong, so
            # nothing may filter on these.
            stages="; ".join(stages) if isinstance(stages, list) else (str(stages) if stages else None),
            verticals="; ".join(verticals) if isinstance(verticals, list) else (str(verticals) if verticals else None),
            year_founded=str(r.get("year_founded") or "") or None,
            status=status or None,
            fund=fund,
        ))
    return out


# ------------------------------------------------------------ list_plus_detail


def parse_list_plus_detail(page: str, spec: dict[str, Any], fund: str,
                           *, base: str, fetch_details: bool = True,
                           limit: int | None = None) -> list[PortfolioCompany]:
    pattern = spec.get("link_pattern", r'href="(/companies/[^"#?]+)"')
    links = sorted(set(re.findall(pattern, page)))
    if limit:
        links = links[:limit]
    out = []
    for href in links:
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        company = PortfolioCompany(
            name=slug.replace("-", " ").title(),
            detail_url=href if href.startswith("http") else base.rstrip("/") + href,
            fund=fund,
        )
        if fetch_details:
            try:
                detail = fetch(company.detail_url, cache_key=f"{fund}-{slug}")
                _enrich_from_detail(company, detail, spec)
            except FundError:
                pass
        out.append(company)
    return out


def _enrich_from_detail(company: PortfolioCompany, page: str, spec: dict[str, Any]) -> None:
    """Pull the real domain and any named people off a detail page."""
    for m in re.finditer(r'href="(https?://[^"]+)"', page):
        url = m.group(1)
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
        if any(skip in host for skip in spec.get("skip_hosts", [])):
            continue
        company.domain_url = url
        break
    desc = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', page)
    if desc:
        company.description = html.unescape(desc.group(1))
    title = re.search(r"<title>([^<]+)</title>", page)
    if title and company.name.lower() in ("", title.group(1).lower()):
        company.name = html.unescape(title.group(1)).split("|")[0].strip()


# ------------------------------------------------------------------ dispatch


STRATEGIES = {"embedded_json", "list_plus_detail"}


def extract(fund_name: str, spec: dict[str, Any], *, force: bool = False,
            limit: int | None = None) -> list[PortfolioCompany]:
    strategy = spec.get("strategy")
    if strategy not in STRATEGIES:
        raise FundError(
            f"fund {fund_name!r} has strategy {strategy!r}; known: {sorted(STRATEGIES)}"
        )
    url = spec.get("url")
    if not url:
        raise FundError(f"fund {fund_name!r} has no url")
    page = fetch(url, cache_key=fund_name, force=force)

    if len(page) < 2000:
        raise FundError(
            f"{url} returned only {len(page)} bytes. Check the raw response before "
            f"concluding the fund has no portfolio."
        )
    if strategy == "embedded_json":
        return parse_embedded_json(page, spec, fund_name)
    base = re.match(r"^https?://[^/]+", url).group(0)
    return parse_list_plus_detail(page, spec, fund_name, base=base, limit=limit)
