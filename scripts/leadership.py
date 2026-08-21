"""Is this person on the company's founders or leadership page?

The one filter the operator asked for by name, and the cheap check whose absence
they said they would regret. Everything else about a record can be wrong in a way
review catches; a cold sequence to a founder is wrong in a way that is only
visible after it has been sent.

Deliberately conservative: a name that appears anywhere on a page whose URL or
heading says leadership, team, about or founders counts as a hit. Over-flagging
costs one row read twice. Under-flagging costs the thing being guarded against.
"""

from __future__ import annotations

import re

from .homepages import fetch_one, visible_text

PATHS = ("/about", "/team", "/leadership", "/company", "/about-us", "/our-team",
         "/company/about", "/about/team", "/people")

TITLE_NEAR = re.compile(
    r"\b(founder|co-?founder|chief|c[et]o\b|ceo\b|cto\b|coo\b|president|"
    r"vp\b|vice president|head of|director|partner|executive)\b", re.I)


def _name_on(text: str, name: str) -> bool:
    parts = [p for p in re.split(r"[^A-Za-z]+", name.lower()) if len(p) > 1]
    if len(parts) < 2:
        return False
    low = text.lower()
    # Both tokens, and within a short span of each other, so two unrelated
    # mentions on a long page do not read as one person's name.
    for m in re.finditer(re.escape(parts[0]), low):
        if parts[-1] in low[m.start():m.start() + 60]:
            return True
    return False


def scan(domain: str, names: list[str], timeout: int = 15) -> dict[str, str]:
    """name -> the URL and context where it appeared. Missing means not found."""
    hits: dict[str, str] = {}
    remaining = [n for n in names]
    for path in PATHS:
        if not remaining:
            break
        url = f"https://{domain.rstrip('/')}{path}"
        r = fetch_one(url, timeout=timeout)
        if r.status not in ("ok", "js_shell") or not r.raw:
            continue
        text = " ".join((r.text or visible_text(r.raw)).split())
        still = []
        for name in remaining:
            if not _name_on(text, name):
                still.append(name)
                continue
            idx = text.lower().find(name.split()[0].lower())
            window = text[max(0, idx - 90):idx + 120]
            near = TITLE_NEAR.search(window)
            hits[name] = (f"{url} -- {window.strip()[:180]}"
                          + (f" [role word: {near.group(0)}]" if near else ""))
        remaining = still
    return hits
