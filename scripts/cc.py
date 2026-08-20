"""CC / BCC resolution.

One file controls who is copied on every email. Resolution is most-specific
first: domain -> campaign -> step -> default.

Two modes:
  merge: false  the most specific level that *defines* a list wins outright
  merge: true   every matching level is unioned with the default

The distinction between "this level says nothing" and "this level says empty
list" is load-bearing: `by_step: {step3_breakup: {cc: []}}` means drop the CC on
the breakup email, and it must not fall through to the default.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .config import CCConfig, CCRule
from .normalize import registrable_domain
from .suppression import filter_addresses

LEVELS = ("domain", "campaign", "step", "default")


@dataclass
class Resolved:
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    cc_source: str = "default"
    bcc_source: str = "default"
    suppressed: list[str] = field(default_factory=list)

    @property
    def recipient_count(self) -> int:
        """Recipients on the message, excluding the To: address."""
        return len(self.cc) + len(self.bcc)


def _rules(config: CCConfig, domain: str | None, campaign: str | None, step: str | None):
    """Yield (level_name, rule) most-specific first, skipping absent levels."""
    if domain:
        for key in (domain.lower(), registrable_domain(domain)):
            rule = config.by_domain.get(key)
            if rule:
                yield f"domain:{key}", rule
                break
    if campaign and (rule := config.by_campaign.get(campaign)):
        yield f"campaign:{campaign}", rule
    if step and (rule := config.by_step.get(step)):
        yield f"step:{step}", rule
    yield "default", config.default


def resolve(
    config: CCConfig,
    *,
    domain: str | None = None,
    campaign: str | None = None,
    step: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> Resolved:
    """Resolve the CC/BCC lists for one send.

    If `conn` is given, suppressed addresses are stripped: a CC address goes
    through the same suppression check as a recipient, and never appears on an
    email to a suppressed domain.
    """
    out = Resolved()
    matches = list(_rules(config, domain, campaign, step))

    if config.merge:
        cc: list[str] = []
        bcc: list[str] = []
        sources: list[str] = []
        for level, rule in matches:
            if rule.cc or rule.bcc:
                sources.append(level)
            cc.extend(rule.cc or [])
            bcc.extend(rule.bcc or [])
        out.cc, out.bcc = _dedupe(cc), _dedupe(bcc)
        out.cc_source = out.bcc_source = "merge(" + ",".join(sources) + ")" if sources else "none"
    else:
        for level, rule in matches:
            if rule.cc is not None:
                out.cc, out.cc_source = _dedupe(rule.cc), level
                break
        for level, rule in matches:
            if rule.bcc is not None:
                out.bcc, out.bcc_source = _dedupe(rule.bcc), level
                break

    if conn is not None:
        out.cc, blocked_cc = filter_addresses(conn, out.cc)
        out.bcc, blocked_bcc = filter_addresses(conn, out.bcc)
        out.suppressed = blocked_cc + blocked_bcc

    return out


def _dedupe(addresses: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in addresses:
        a = a.strip().lower()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out
