"""Find a researcher's personal page and read an address off it.

The channel decision behind this module: a commit email is an address without an
identity -- it proves someone once pushed code, not who they are now. A personal
or lab page carries name, title, research area, publications and often an
address in one document, all first-party. So this guesses the small number of
URL shapes researchers actually use, fetches them, and reads what is there.

Guessing URLs is cheap and wrong most of the time, which is fine: a miss costs
one HTTP request, and a hit costs nothing and produces a fully grounded record.
What it must never do is guess an *address*. Everything returned here is read
off a page, with the URL it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .homepages import fetch_one, visible_text

# Addresses written to defeat scrapers. Researchers do this constantly, and a
# reader that only understands mailto: misses most academic pages.
OBFUSCATED = [
    re.compile(r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\s+at\s+|&#64;|\{at\})\s*"
               r"([A-Za-z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+|\{dot\})\s*([A-Za-z]{2,})",
               re.I),
    re.compile(r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|&#64;|\{at\})\s*"
               r"([A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.I),
]
PLAIN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO = re.compile(r'mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})', re.I)

JUNK = ("example.com", "domain.com", "yourname", "sentry.io", "wixpress",
        "squarespace", "godaddy", "@2x", "email.com", "gmail.com.")


@dataclass
class PersonPage:
    name: str
    url: str | None = None
    status: str = "not_found"   # found | namesake_risk | no_email | not_found | blocked
    emails: list[str] = field(default_factory=list)
    title_line: str | None = None
    corroborated: bool = False
    tried: list[str] = field(default_factory=list)


def candidate_urls(name: str) -> list[str]:
    """URL shapes researchers actually use, most likely first."""
    parts = [p for p in re.split(r"[^A-Za-z]+", name.strip().lower()) if p]
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]
    stems = [f"{first}{last}", f"{first}-{last}", f"{first}_{last}", last]
    urls = [f"https://{s}.github.io/" for s in stems[:3]]
    urls += [f"https://{first}{last}.com/", f"https://{first}{last}.me/",
             f"https://{first}{last}.ai/", f"https://{last}.io/"]
    return urls


def emails_on(page: str, text: str) -> list[str]:
    found: list[str] = []
    found += MAILTO.findall(page)
    for rx in OBFUSCATED:
        for m in rx.findall(text):
            found.append(f"{m[0]}@{m[1]}.{m[2]}" if len(m) == 3 else f"{m[0]}@{m[1]}")
    found += PLAIN.findall(text)
    out, seen = [], set()
    for e in found:
        e = e.strip().lower().rstrip(".")
        if e in seen or any(j in e for j in JUNK) or len(e) > 60:
            continue
        seen.add(e)
        out.append(e)
    return out


def _looks_like(name: str, text: str) -> bool:
    """Does this page belong to the person we asked about?

    A guessed URL can land on a stranger with a similar handle. Requiring both
    name tokens in the visible text is the cheapest defense, and the failure it
    prevents -- emailing the wrong person entirely -- is the worst one here.
    """
    parts = [p for p in re.split(r"[^A-Za-z]+", name.lower()) if len(p) > 1]
    low = text.lower()
    return all(p in low for p in (parts[0], parts[-1])) if len(parts) >= 2 else False


def find(name: str, extra_urls: list[str] | None = None,
         company: str | None = None) -> PersonPage:
    """Probe for a page. If `company` is given, require the page to mention it.

    Name matching alone is not enough. Probing "Pankaj Gupta" found a real page
    belonging to a different Pankaj Gupta and read a stranger's address off it.
    Both name tokens were present, so every check passed and the result looked
    clean. Requiring the employer to appear too is what separates "a page about
    someone with this name" from "this person's page".
    """
    out = PersonPage(name=name)
    for url in (extra_urls or []) + candidate_urls(name):
        out.tried.append(url)
        r = fetch_one(url)
        if r.status not in ("ok", "js_shell") or not r.raw:
            continue
        text = r.text or visible_text(r.raw)
        if not _looks_like(name, text):
            continue
        out.url = r.final_url or url
        out.corroborated = bool(company) and company.lower().split()[0] in text.lower()
        out.status = "no_email"
        emails = emails_on(r.raw, text)
        if emails:
            # An address off a page that never names the employer we are
            # sourcing for is a namesake until something says otherwise. It is
            # returned, so the operator can see it, but never as "found".
            out.emails = emails
            out.status = "found" if (out.corroborated or not company) else "namesake_risk"
        for line in text.splitlines():
            line = line.strip()
            if 12 < len(line) < 140 and name.split()[-1].lower() not in line.lower():
                out.title_line = line
                break
        return out
    return out
