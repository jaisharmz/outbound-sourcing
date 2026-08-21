"""Paid contact-enrichment providers. Interface only -- nothing is implemented.

RocketReach, Apollo, Clearbit and similar sell exactly what the investigation
loop spends its time deriving: an address for a named person at a named company.
They are a legitimate shortcut, but they change the evidence story, so the seam
is defined here and left empty until the operator decides to add a key.

What a provider must return to be usable by this system:

  An address is not enough. `resolve()` returns Evidence-shaped records with a
  source URL and a quote, because a contact whose grounding is "a vendor said
  so" cannot be reviewed -- there is nothing for the reviewer to check. A
  provider that only returns a string should be recorded with the provider's own
  result page as the URL and the payload as the quote, so the claim at least
  names who made it.

  Provenance survives into the record. `email_basis` becomes "purchased", which
  is neither "observed" nor "inferred_from_pattern": the address was not seen on
  a page the person controls, and it was not derived from a pattern this system
  measured. The review gate should flag it as such and let the operator decide.

To add one: implement Provider, register it in PROVIDERS, put the key in
secrets.env, and name it in campaign.yaml. The loop will reach for it only after
free channels are exhausted for a given person, because a paid lookup that the
loop could have derived for nothing is money spent to skip evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class EnrichmentNotConfigured(RuntimeError):
    """No paid provider is configured. Never raised during a normal run."""


@dataclass
class EnrichmentResult:
    email: str | None
    title: str | None
    source_url: str
    quote: str
    provider: str
    confidence: float = 0.0


class Provider(Protocol):
    name: str

    def resolve(self, full_name: str, company: str,
                domain: str | None = None) -> EnrichmentResult | None:
        """Look one person up. Return None when the provider has nothing."""


PROVIDERS: dict[str, Provider] = {}


def available() -> list[str]:
    return sorted(PROVIDERS)


def resolve(full_name: str, company: str, domain: str | None = None,
            provider: str | None = None) -> EnrichmentResult | None:
    """Try a configured provider. Returns None when none is configured."""
    if not PROVIDERS:
        return None
    chosen = PROVIDERS.get(provider) if provider else next(iter(PROVIDERS.values()))
    return chosen.resolve(full_name, company, domain) if chosen else None
