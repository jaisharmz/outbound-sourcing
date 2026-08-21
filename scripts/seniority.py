"""Rank a title by how likely the person is to be worth a cold email.

The ordering is deliberately the inverse of an org chart. A co-founder or chief
scientist is the profile *least* likely to reply: their inbox is full, their
calendar is not theirs, and an outside research collaboration is not a decision
they make personally -- it routes through a process. A research scientist or
member of technical staff does the work the pitch is actually about, has room to
say yes to a conversation, and is reachable.

So leadership sorts last. It is not excluded: a company that yields only a
founder is still worth a considered email, and the operator said as much. It is
ranked below plain IC titles and flagged at review so approving one is a
deliberate act rather than a default.

Ranks are ordered best-first; lower number sorts earlier.
"""

from __future__ import annotations

import re

IC_RESEARCH = 0      # does the work the pitch is about
IC_SENIOR = 1        # senior IC: still hands-on, more scattered
LEADERSHIP = 2       # runs the org; least likely to reply
UNKNOWN = 3

RANK_NAMES = {IC_RESEARCH: "ic_research", IC_SENIOR: "ic_senior",
              LEADERSHIP: "leadership", UNKNOWN: "unknown"}

# Matched as whole phrases against a lowercased title. Leadership is checked
# first: "Co-founder and Chief Scientist" contains "scientist", and matching IC
# first would rank the most senior person on the list top.
LEADERSHIP_PATTERNS = (
    r"\bfounder\b", r"\bco-?founder\b", r"\bc[et]o\b", r"\bceo\b", r"\bcoo\b",
    r"\bcso\b", r"\bchief\b", r"\bpresident\b", r"\bpartner\b",
    r"\bvp\b", r"\bvice president\b", r"\bdirector\b", r"\bhead of\b",
    r"\bmanager\b", r"\blead of\b", r"\bexecutive\b", r"\bowner\b",
)
IC_SENIOR_PATTERNS = (
    r"\bprincipal\b", r"\bdistinguished\b", r"\bstaff\b", r"\barchitect\b",
    r"\bfellow\b", r"\bsenior staff\b",
)
# Explicit IC titles, checked before IC_SENIOR. "Member of Technical Staff"
# contains "staff", and the bare-staff rule would file the canonical frontier-lab
# IC title as senior-scattered, which is exactly backwards.
IC_EXPLICIT_PATTERNS = (
    r"\bmember of technical staff\b", r"\bmts\b", r"\bresearch scientist\b",
    r"\bresearch engineer\b", r"\bapplied scientist\b", r"\bresearcher\b",
)
IC_RESEARCH_PATTERNS = (
    r"\bresearch scientist\b", r"\bresearch engineer\b", r"\bresearcher\b",
    r"\bmember of technical staff\b", r"\bmts\b", r"\bapplied scientist\b",
    r"\bscientist\b", r"\bengineer\b", r"\bpostdoc", r"\bpost-?doctoral\b",
    r"\bphd student\b", r"\bprofessor\b", r"\bmachine learning\b",
)


def rank(title: str | None) -> int:
    t = (title or "").lower()
    if not t.strip():
        return UNKNOWN
    if any(re.search(p, t) for p in LEADERSHIP_PATTERNS):
        return LEADERSHIP
    if any(re.search(p, t) for p in IC_EXPLICIT_PATTERNS):
        return IC_RESEARCH
    if any(re.search(p, t) for p in IC_SENIOR_PATTERNS):
        return IC_SENIOR
    if any(re.search(p, t) for p in IC_RESEARCH_PATTERNS):
        return IC_RESEARCH
    return UNKNOWN


def name(title: str | None) -> str:
    return RANK_NAMES[rank(title)]


def is_leadership(title: str | None) -> bool:
    return rank(title) == LEADERSHIP


def explain(title: str | None) -> str:
    r = rank(title)
    if r == LEADERSHIP:
        return ("a founder/exec title: least likely to reply, and an outside "
                "collaboration is a process decision rather than a personal one")
    if r == IC_SENIOR:
        return "a senior individual contributor: hands-on, but more scattered"
    if r == IC_RESEARCH:
        return "an individual contributor who does the work the pitch is about"
    return "title does not classify; treat as unranked"
