"""Deterministic normalization. No judgment, no model, no network."""

from __future__ import annotations

import re
import unicodedata

LEGAL_SUFFIXES = (
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "gmbh", "sa", "s.a.", "bv", "b.v.", "ag", "plc",
    "pbc", "labs", "lab", "ai", "technologies", "technology", "holdings",
)

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
    while tokens and tokens[-1] in LEGAL_SUFFIXES and len(tokens) > 1:
        tokens.pop()
    return " ".join(tokens)


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
    while tokens and tokens[-1].lower().strip(".") in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) if tokens else name.strip()
