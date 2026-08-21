"""Verify that every linked document resolves without authentication.

A request-access wall is worse than no link: the recipient clicks, is told to
ask permission, and the email reads as careless. Format is checked at config
load, which is fast and works offline; reachability is checked here, because a
network call on every config load would be neither.

The result is a gate. A campaign whose links have never passed, or whose links
changed since they last passed, refuses to send.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import ssl
import urllib.error
import urllib.request

from .config import Config
from .db import log_event, utcnow

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

LOGIN_MARKERS = ("accounts.google.com", "signin", "sign in to continue")
WALL_MARKERS = ("request access", "you need access", "access denied",
                "you need permission", "restricted")


def check_url(url: str, timeout: int = 30) -> tuple[str, str]:
    """Return (status, detail). ok | login_wall | permission_wall | dead."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            head = r.read(6000)
            final = r.geturl()
            disposition = r.headers.get("Content-Disposition", "")
    except urllib.error.HTTPError as exc:
        return "dead", f"HTTP {exc.code}"
    except Exception as exc:
        return "dead", type(exc).__name__

    low = head.decode("utf-8", "replace").lower()
    if any(m in final.lower() for m in LOGIN_MARKERS):
        return "login_wall", f"redirected to a sign-in page ({final[:70]})"
    if any(m in low for m in WALL_MARKERS):
        return "permission_wall", "the page asks the visitor to request access"
    if disposition or head[:4] == b"%PDF":
        name = re.search(r'filename="([^"]+)"', disposition)
        return "ok", f"serves a file directly{f' ({name.group(1)})' if name else ''}"
    if "virus scan warning" in low or "can't scan" in low:
        return "ok", "serves the file behind Drive's size-based scan notice"
    return "dead", f"returned {len(head)} bytes of HTML with no file"


def links_fingerprint(config: Config, campaign: str | None = None) -> str:
    urls = sorted({d.url for a in config.sequence.attachment_sets.values()
                   for d in a.documents if d.url})
    for step in config.steps_for(campaign):
        urls.extend(sorted(step.links.values()))
    return hashlib.sha256("|".join(sorted(set(urls))).encode()).hexdigest()[:16]


def check_all(conn: sqlite3.Connection, config: Config,
              campaign: str | None = None) -> list[tuple[str, str, str, str]]:
    """Check every linked document. Returns (name, url, status, detail)."""
    seen: dict[str, str] = {}
    for aset in config.sequence.attachment_sets.values():
        for d in aset.documents:
            if d.url:
                seen[d.url] = d.name
    for step in config.steps_for(campaign):
        for name, url in step.links.items():
            seen[url] = name

    results = []
    for url, name in seen.items():
        status, detail = check_url(url)
        results.append((name, url, status, detail))
        conn.execute(
            "INSERT INTO link_checks (name, url, status, detail, fingerprint, checked_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, url, status, detail, links_fingerprint(config, campaign), utcnow()))
    log_event(conn, "info", "links.check", checked=len(results),
              failed=sum(1 for r in results if r[2] != "ok"))
    return results


def gate(conn: sqlite3.Connection, config: Config, campaign: str | None = None) -> list[str]:
    """Blockers arising from links: never checked, changed since, or failing."""
    fp = links_fingerprint(config, campaign)
    rows = conn.execute(
        "SELECT name, url, status, detail, fingerprint FROM link_checks"
        " WHERE id IN (SELECT MAX(id) FROM link_checks GROUP BY url)").fetchall()
    if not rows:
        return ["linked documents have never been checked. Run: outbound check-links"]
    stale = [r for r in rows if r["fingerprint"] != fp]
    if stale or len(rows) < len(set(
            [d.url for a in config.sequence.attachment_sets.values() for d in a.documents if d.url])):
        return ["linked documents changed since they were last checked. "
                "Run: outbound check-links"]
    return [f"{r['name']} link is not publicly reachable ({r['status']}): {r['detail']}"
            for r in rows if r["status"] != "ok"]
