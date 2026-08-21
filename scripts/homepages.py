"""Fetch each company's homepage and keep what it says about itself.

A portfolio blurb is the investor's copy. The company's own homepage is better
evidence for stage 0, and it cannot return a different company with the same
name the way a search can, because we already hold the domain.

Marketing sites are the worst case for the standing rule in
references/discovery.md: a JS-rendered shell, a parked domain and a real page
with little text all come back as HTTP 200 with a plausible-looking body. So
every fetch records *why* it produced what it did, and a site that did not
render is `unknown`, never `fail`.
"""

from __future__ import annotations

import concurrent.futures
import gzip
import html
import io
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MIN_MEANINGFUL_CHARS = 220

HOLDING_MARKERS = (
    "coming soon", "under construction", "this domain is for sale", "parked",
    "buy this domain", "website is being updated", "launching soon",
    "account suspended", "default web page", "index of /",
)
BLOCKED_MARKERS = (
    "just a moment", "checking your browser", "attention required",
    "access denied", "captcha", "verify you are human", "enable javascript and cookies",
)
# Signals that the body is a client-rendered shell rather than a thin page.
SHELL_MARKERS = (
    '__next_data__', 'id="root"', 'id="__next"', 'id="app"', 'ng-app',
    'data-reactroot', 'window.__nuxt__',
)


@dataclass
class HomepageResult:
    url: str
    status: str           # ok | js_shell | holding | dead | blocked
    text: str = ""
    detail: str = ""
    # Raw HTML, kept because mailto: links live in markup and are erased by
    # visible-text extraction -- the same trap as reading a page through a
    # markdown converter and concluding the data is not there.
    raw: str = ""
    final_url: str = ""


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "meta":
            a = dict(attrs)
            if a.get("name") in ("description", "og:description") or \
               a.get("property") == "og:description":
                if a.get("content"):
                    self.parts.append(a["content"])

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            s = data.strip()
            if s:
                self.parts.append(s)


def visible_text(page: str) -> str:
    p = _Text()
    try:
        p.feed(page)
    except Exception:
        pass
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


def embedded_text(page: str) -> str:
    """Recover copy from framework payloads before calling a page empty.

    The standing rule: a page that renders thin may still carry its content in
    the source. Check before concluding anything.
    """
    found: list[str] = []
    for m in re.finditer(r'<script[^>]*type="application/(?:ld\+)?json"[^>]*>(.*?)</script>',
                         page, re.S | re.I):
        found.append(m.group(1))
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m:
        found.append(m.group(1))
    out: list[str] = []
    for blob in found:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue

        def walk(o):
            if isinstance(o, str) and len(o) > 40:
                out.append(o)
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o[:60]:
                    walk(v)

        walk(data)
    for m in re.finditer(r'data-[a-z-]+="(\[[^"]{200,}\])"', page):
        out.append(html.unescape(m.group(1))[:4000])
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def classify(page: str, text: str, embedded: str) -> tuple[str, str]:
    low = page[:6000].lower()
    if any(m in low for m in BLOCKED_MARKERS):
        return "blocked", "bot challenge or refusal in the body"
    body = text if len(text) >= len(embedded) else embedded
    tl = body.lower()
    if body and len(body) < 600 and any(m in tl for m in HOLDING_MARKERS):
        return "holding", "parked or coming-soon page"
    if len(body) >= MIN_MEANINGFUL_CHARS:
        return "ok", "embedded payload" if body is embedded else "rendered text"
    if any(m in low for m in SHELL_MARKERS) or page.count("<script") > 8:
        return "js_shell", f"client-rendered; only {len(body)} chars of text in source"
    if len(page) < 900:
        return "holding", f"page is {len(page)} bytes"
    return "js_shell", f"only {len(body)} chars of text extracted from {len(page)} bytes"


def fetch_one(url: str, timeout: int = 20) -> HomepageResult:
    if not url:
        return HomepageResult(url, "dead", detail="no url")
    if not url.startswith("http"):
        url = "https://" + url
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # expired certs are common and not our problem
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(3_000_000)
            if resp.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except OSError:
                    pass
            page = raw.decode("utf-8", errors="replace")
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        status = "blocked" if exc.code in (401, 403, 406, 429) else "dead"
        return HomepageResult(url, status, detail=f"HTTP {exc.code}")
    except Exception as exc:
        return HomepageResult(url, "dead", detail=type(exc).__name__)

    text = visible_text(page)
    embedded = embedded_text(page)
    status, detail = classify(page, text, embedded)
    body = text if len(text) >= len(embedded) else embedded
    return HomepageResult(url, status, text=body[:8000], detail=detail,
                          raw=page[:400_000], final_url=final_url)


def fetch_many(rows: list[tuple[int, str]], workers: int = 8) -> dict[int, HomepageResult]:
    out: dict[int, HomepageResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, url): aid for aid, url in rows}
        for fut in concurrent.futures.as_completed(futures):
            aid = futures[fut]
            try:
                out[aid] = fut.result()
            except Exception as exc:
                out[aid] = HomepageResult("", "dead", detail=type(exc).__name__)
    return out
