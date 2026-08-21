"""Deterministic normalization. No judgment, no model, no network."""

from __future__ import annotations

import re
import unicodedata

# Actual legal suffixes. Safe to remove from a name in email copy: nobody writes
# "your team at Kepler Systems, Inc." in a sentence.
LEGAL_SUFFIXES = (
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "gmbh", "sa", "s.a.", "bv", "b.v.", "ag", "plc",
    "pbc", "incorporated", "incorporated.",
)

# Additionally folded away when building a dedupe key, so "Vals AI" and "Vals"
# match. Never removed for display: "your team at Together" is wrong, and
# stripping "AI" from an AI company's name is the specific way it goes wrong.
NORMALIZE_ONLY_SUFFIXES = ("labs", "lab", "ai", "technologies", "technology")

# Words that are part of a brand and must survive display trimming even if they
# resemble a suffix. Checked before anything is removed.
DISPLAY_KEEP = {
    "ai", "labs", "lab", "systems", "technologies", "technology", "research",
    "health", "bio", "robotics", "computing", "dynamics", "networks", "security",
    "intelligence", "sciences", "science", "works", "studio", "studios", "space",
    # Reads as part of the brand rather than a legal form: "Proof Holdings"
    # trimmed to "Proof" is a different company's name.
    "holdings",
}

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
    "qq.com", "163.com", "mail.com", "yandex.com",
}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_company(name: str) -> str:
    """A stable key for cross-company dedupe. 'Anthropic, PBC' -> 'anthropic'."""
    s = strip_accents(name).lower().strip()
    s = re.sub(r"[^a-z0-9\s\-&]", " ", s)
    tokens = [t for t in s.split() if t]
    foldable = LEGAL_SUFFIXES + NORMALIZE_ONLY_SUFFIXES
    while tokens and tokens[-1] in foldable and len(tokens) > 1:
        tokens.pop()
    return " ".join(tokens)


# One definition each. Both of these were duplicated across modules that grew
# their own copy, and a regex that exists twice drifts: the second copy is the
# one nobody remembers to fix.
EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def name_key(name: str) -> str:
    """Sorted lowercase tokens, so reversed name order matches one person.

    OpenAlex carries both "Thomas Wolf" and "Wolf Thomas" for the same author.
    Distinct from normalize_person, which strips honorifics and keeps order --
    that one answers "is this the same human", this one answers "is this the
    same name written differently".
    """
    return " ".join(sorted(t for t in re.split(r"[^a-z]+", (name or "").lower()) if t))


def normalize_person(name: str) -> str:
    """'Dr. Jane Q. Doe III' -> 'jane doe'. Used to catch the same human twice."""
    s = strip_accents(name).lower()
    s = re.sub(r"[^a-z\s\-']", " ", s)
    tokens = [t for t in s.split() if t]
    drop_lead = {"dr", "prof", "professor", "mr", "ms", "mrs", "mx"}
    drop_trail = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "msc", "bsc"}
    while tokens and tokens[0] in drop_lead:
        tokens.pop(0)
    while tokens and tokens[-1] in drop_trail:
        tokens.pop()
    tokens = [t for t in tokens if len(t) > 1]  # middle initials
    if len(tokens) > 2:
        tokens = [tokens[0], tokens[-1]]
    return " ".join(tokens)


def normalize_email(email: str) -> str:
    """Lowercase, and fold Gmail dots/plus-tags so one human is one row."""
    email = email.strip().lower()
    local, _, domain = email.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    else:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def domain_of(email: str) -> str:
    return email.strip().lower().partition("@")[2]


def registrable_domain(domain: str) -> str:
    """Good-enough eTLD+1 for grouping subdomains. Not a public-suffix parser."""
    parts = domain.strip().lower().strip(".").split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    two = ".".join(parts[-2:])
    multi = {"co.uk", "ac.uk", "co.jp", "com.au", "co.in", "com.br", "ac.il", "edu.au"}
    return ".".join(parts[-3:]) if two in multi else two


def is_free_mail(domain: str) -> bool:
    return registrable_domain(domain) in FREE_MAIL


def first_name_of(full_name: str) -> str:
    tokens = normalize_person(full_name).split()
    return tokens[0].capitalize() if tokens else full_name.strip()


def display_company(name: str) -> str:
    """The name to put in an email body, with its legal suffix removed.

    The record keeps "Kepler Systems, Inc." because that is what the evidence
    says. A sentence ending "...your group at Kepler Systems, Inc.." does not
    survive a human reading it, so copy gets "Kepler Systems".
    """
    s = name.strip().rstrip(".,")
    tokens = s.replace(",", " ").split()
    while tokens:
        last = tokens[-1].lower().strip(".")
        if last in DISPLAY_KEEP or last not in LEGAL_SUFFIXES:
            break
        tokens.pop()
    return " ".join(tokens) if tokens else name.strip()
