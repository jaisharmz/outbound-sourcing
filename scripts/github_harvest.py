"""Harvest observed email addresses from public GitHub commits.

Extracting an address from a commit is mechanism, not judgment, so it belongs in
a script. Its real value is not the addresses themselves but the pattern they
establish: one confirmed `first.last@` at a domain unlocks every name found
anywhere else.

Three things this is careful about, each because the naive version misleads:

**Throttling is not absence.** An unauthenticated probe returned "no repos" for
78 of 88 companies, which looked like a finding and was actually a rate limit.
Every outcome here is a named status, and `throttled` is never reported as
`no_public_repos`.

**A commit proves an address existed, not that the person is still there.** The
commit date is recorded, and anything older than the staleness window is pattern
evidence only -- never a sendable contact. Anyscale is the cautionary case: three
findable founders at a company acquired three weeks earlier.

**Bots and noreply addresses are not people.** They are filtered before anything
downstream can treat them as candidates.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
STALE_AFTER_DAYS = 730          # ~2 years

NOREPLY_MARKERS = (
    "users.noreply.github.com", "noreply", "no-reply", "donotreply",
)
BOT_MARKERS = (
    "dependabot", "renovate", "github-actions", "greenkeeper", "snyk-bot",
    "semantic-release", "codecov", "allcontributors", "[bot]", "svc-", "-bot@",
    "admin@", "ci@", "build@", "release@", "automation@",
)


def is_person_address(email: str, name: str = "") -> bool:
    e, n = email.lower(), (name or "").lower()
    if any(m in e for m in NOREPLY_MARKERS):
        return False
    if any(m in e or m in n for m in BOT_MARKERS):
        return False
    return "@" in e


# ------------------------------------------------------------------ transport


class RateLimiter:
    """Reads the budget off each response instead of guessing at it."""

    def __init__(self, floor: int = 25):
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.floor = floor
        self.throttled = False

    def note(self, headers) -> None:
        try:
            self.remaining = int(headers.get("X-RateLimit-Remaining", ""))
            self.reset_at = float(headers.get("X-RateLimit-Reset", ""))
        except (TypeError, ValueError):
            pass

    def should_pause(self) -> float:
        """Seconds to wait, or 0. Stops well before zero so a run degrades
        gracefully rather than turning into a wall of false negatives."""
        if self.remaining is None or self.remaining > self.floor:
            return 0.0
        if not self.reset_at:
            return 0.0
        return max(0.0, self.reset_at - time.time()) + 2


@dataclass
class Client:
    token: str | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)
    calls: int = 0

    def get(self, path: str) -> tuple[object | None, str]:
        """Returns (payload, status). Status is one of ok | throttled | missing | error."""
        wait = self.limiter.should_pause()
        if wait:
            if wait > 900:                       # do not sit for a quarter hour
                self.limiter.throttled = True
                return None, "throttled"
            time.sleep(wait)

        headers = {"User-Agent": "outbound-sourcing/0.1",
                   "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(path if path.startswith("http") else API + path,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                self.calls += 1
                self.limiter.note(resp.headers)
                return json.loads(resp.read()), "ok"
        except urllib.error.HTTPError as exc:
            self.limiter.note(exc.headers)
            if exc.code == 404:
                return None, "missing"
            if exc.code in (403, 429):
                self.limiter.throttled = True
                return None, "throttled"
            return None, "error"
        except Exception:
            return None, "error"


# ------------------------------------------------------------------ harvest


@dataclass
class DomainResult:
    company: str
    domain: str
    org: str | None = None
    status: str = "not_started"
    # email -> (name, most recent commit ISO date)
    addresses: dict[str, tuple[str, str]] = field(default_factory=dict)
    filtered: int = 0
    repos_seen: int = 0
    archived_repos: int = 0

    @property
    def newest_commit_at(self) -> str | None:
        dates = [when for _n, when in self.addresses.values() if when]
        return max(dates) if dates else None

    @property
    def fresh(self) -> dict[str, tuple[str, str]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)
        out = {}
        for em, (nm, when) in self.addresses.items():
            try:
                if datetime.fromisoformat(when.replace("Z", "+00:00")) >= cutoff:
                    out[em] = (nm, when)
            except ValueError:
                continue
        return out

    @property
    def stale(self) -> dict[str, tuple[str, str]]:
        fresh = self.fresh
        return {k: v for k, v in self.addresses.items() if k not in fresh}


def resolve_org(client: Client, company: str, domain: str) -> tuple[str | None, str]:
    """Find the org whose homepage matches the domain. Falls back to the stem."""
    stem = domain.split(".")[0]
    detail, status = client.get(f"/orgs/{stem}")
    if status == "throttled":
        return None, "throttled"
    if status == "ok" and isinstance(detail, dict):
        return detail.get("login"), "ok"

    q = urllib.parse.quote(f"{company} type:org")
    res, status = client.get(f"/search/users?q={q}&per_page=5")
    if status == "throttled":
        return None, "throttled"
    for item in (res or {}).get("items", []) if isinstance(res, dict) else []:
        detail, status = client.get(item["url"])
        if status == "throttled":
            return None, "throttled"
        if isinstance(detail, dict) and domain in (detail.get("blog") or "").lower():
            return detail.get("login"), "ok"
    return None, "no_org_found"


def harvest_domain(client: Client, company: str, domain: str, *,
                   repos: int = 4, per_repo: int = 100) -> DomainResult:
    out = DomainResult(company=company, domain=domain)
    org, status = resolve_org(client, company, domain)
    if status == "throttled":
        out.status = "throttled"
        return out
    if not org:
        out.status = "no_org_found"
        return out
    out.org = org

    repo_list, status = client.get(f"/orgs/{org}/repos?per_page={repos}&sort=pushed")
    if status == "throttled":
        out.status = "throttled"
        return out
    if not isinstance(repo_list, list):
        out.status = "no_public_repos"
        return out
    if not repo_list:
        out.status = "no_public_repos"
        return out

    saw_commits = False
    out.repos_seen = len(repo_list[:repos])
    out.archived_repos = sum(1 for r in repo_list[:repos] if r.get("archived"))
    for repo in repo_list[:repos]:
        commits, status = client.get(
            f"/repos/{repo['full_name']}/commits?per_page={per_repo}")
        if status == "throttled":
            out.status = "throttled" if not out.addresses else "partial_throttled"
            return out
        if not isinstance(commits, list):
            continue
        saw_commits = True
        for c in commits:
            author = (c.get("commit") or {}).get("author") or {}
            email = (author.get("email") or "").strip().lower()
            name = (author.get("name") or "").strip()
            when = author.get("date") or ""
            if not email.endswith("@" + domain):
                continue
            if not is_person_address(email, name):
                out.filtered += 1
                continue
            prev = out.addresses.get(email)
            if not prev or when > prev[1]:
                out.addresses[email] = (name, when)

    out.status = "ok" if out.addresses else ("no_addresses" if saw_commits else "no_public_repos")
    return out


# ------------------------------------------------------------------ patterns


PATTERNS = {
    "first": lambda f, l: f,
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "flast": lambda f, l: f"{f[0]}{l}" if f else "",
    "f.last": lambda f, l: f"{f[0]}.{l}" if f else "",
    "firstl": lambda f, l: f"{f}{l[0]}" if l else "",
    "last": lambda f, l: l,
}


def infer_pattern(addresses: dict[str, tuple[str, str]]) -> tuple[str | None, float, list[str]]:
    """Infer the local-part convention from observed name/address pairs.

    This is the payoff. One confirmed convention turns every name found elsewhere
    into a candidate address, which is most of what discovery is for.
    """
    votes: Counter[str] = Counter()
    used: list[str] = []
    for email, (name, _when) in addresses.items():
        local = email.split("@", 1)[0]
        parts = [p for p in re.split(r"[^a-z]+", (name or "").lower()) if len(p) > 1]
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        matched = [p for p, fn in PATTERNS.items() if fn(first, last) == local]
        if matched:
            used.append(email)
        for p in matched:
            votes[p] += 1
    if not votes:
        return None, 0.0, used
    pattern, n = votes.most_common(1)[0]
    return pattern, n / max(1, len(used)), used


def apply_pattern(pattern: str, full_name: str, domain: str) -> str | None:
    parts = [p for p in re.split(r"[^a-z]+", full_name.lower()) if len(p) > 1]
    if len(parts) < 2 or pattern not in PATTERNS:
        return None
    local = PATTERNS[pattern](parts[0], parts[-1])
    return f"{local}@{domain}" if local else None
