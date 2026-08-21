"""The contract between agentic discovery and the deterministic spine.

Agentic discovery writes only to `state/candidates/<company>.json`. This module
is the gate. Everything downstream reads SQLite and never reads a model's output
directly.

The validator's job is to make an ungrounded claim structurally impossible
rather than something a research subagent is asked nicely to avoid:

  * the name/title/company binding must be supported by evidence carrying a URL
  * the email must be supported by evidence carrying a URL
  * a non-null personalization must carry personalization_source_url
  * emails must parse, and must not be on the suppression list

At 500 sends/day a hallucinated contact is not a bug anyone notices in time.
It is a bounce, and enough of them cost the sending domain permanently.

When a subagent cannot find grounding it emits `personalization: null` and the
template falls back. That is always correct over inventing a detail about
someone's work.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import EMAIL_RE
from .errors import ConfigError, humanize
from .normalize import first_name_of, normalize_company, normalize_person

# Evidence claims are matched to their subject by keyword so the validator can
# tell "this record has two URLs somewhere" from "the email is grounded".
IDENTITY_HINTS = ("works at", "employed", "is a", "title", "role", "affiliation", "member of")


def _tokens(text: str) -> set[str]:
    """Whole words from free text, for evidence matching."""
    return set(re.split(r"[^a-z0-9]+", text.lower())) - {""}


class CandidateError(Exception):
    """A candidate file that cannot be trusted. Never partially ingested."""


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim: str = Field(min_length=3)
    url: str
    quote: str = Field(min_length=1)
    retrieved_at: datetime

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not re.match(r"^https?://[^\s]+\.[^\s]+", v):
            raise ValueError(f"evidence url must be an absolute http(s) URL, got {v!r}")
        return v

    @field_validator("quote")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        if v.strip() in {"...", "N/A", "n/a", "-"}:
            raise ValueError("evidence quote must be the actual text supporting the claim")
        return v


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2)
    title: str = Field(min_length=2)
    company: str = Field(min_length=1)
    email: str
    email_basis: Literal["observed", "inferred_from_pattern"]
    evidence: list[Evidence] = Field(min_length=1)
    personalization: str | None = None
    personalization_source_url: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    # Optional. Used by the ICP filter for region rules.
    country: str | None = None
    linkedin_url: str | None = None
    # The research group, where the person belongs to one. Traversal surfaces a
    # lab's members together, so this is what the per-lab cap and lab-level
    # suppression key on -- without it both silently no-op.
    lab: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError(f"malformed email address: {v!r}")
        return v

    @model_validator(mode="after")
    def _email_is_grounded(self) -> "Candidate":
        local, _, domain = self.email.partition("@")
        grounded = any(
            self.email in ev.claim.lower()
            or self.email in ev.quote.lower()
            or ("email" in ev.claim.lower() and domain in (ev.claim + ev.quote).lower())
            for ev in self.evidence
        )
        if not grounded:
            raise ValueError(
                f"no evidence entry grounds the email {self.email!r}. Every record needs an "
                f"evidence item whose claim or quote contains the address, or which states "
                f"the pattern the address was inferred from, with a URL."
            )
        return self

    @model_validator(mode="after")
    def _identity_is_grounded(self) -> "Candidate":
        # Whole-token matching, not substring. Substring matching let evidence
        # that merely contained "northwindlabs.test" satisfy a binding to
        # "Northwind Labs", which is not the same claim at all. Normalization
        # still absorbs legal suffixes, so evidence naming "Kepler Systems"
        # grounds a record whose company field is "Kepler Systems, Inc.".
        company_tokens = set(normalize_company(self.company).split())
        grounded = any(
            company_tokens <= _tokens(ev.claim + " " + ev.quote)
            and any(hint in ev.claim.lower() for hint in IDENTITY_HINTS)
            for ev in self.evidence
        )
        if not grounded:
            raise ValueError(
                f"no evidence entry binds {self.name!r} to {self.company!r} with a title. "
                f"Expected an evidence claim like 'works at {self.company} as <title>' "
                f"with a URL and a supporting quote."
            )
        return self

    @model_validator(mode="after")
    def _personalization_is_sourced(self) -> "Candidate":
        if self.personalization is not None:
            if not self.personalization_source_url:
                raise ValueError(
                    "personalization is set but personalization_source_url is missing. "
                    "Emit personalization: null instead of an ungrounded detail."
                )
            if not re.match(r"^https?://", self.personalization_source_url):
                raise ValueError("personalization_source_url must be an absolute http(s) URL")
            text = self.personalization.strip()
            if not text[:1].isupper() or text[-1] not in ".!?":
                raise ValueError(
                    "personalization must be one or two complete sentences, starting with a "
                    "capital and ending in punctuation. Templates drop it in as its own "
                    f"paragraph and cannot fix grammar. Got: {text!r}"
                )
        return self

    @property
    def domain(self) -> str:
        return self.email.partition("@")[2]

    @property
    def first_name(self) -> str:
        """Salutation name. Strips honorifics so a template never says "Hello Dr.!"."""
        return first_name_of(self.name)

    @property
    def last_name(self) -> str:
        parts = normalize_person(self.name).split()
        return parts[-1].capitalize() if len(parts) > 1 else ""


class CandidateFile(BaseModel):
    """One company's discovery output. Written by a subagent, read by ingest."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company: str
    domain: str | None = None
    generated_at: datetime
    candidates: list[Candidate] = Field(default_factory=list)
    # Why an empty file is empty. Never pad a thin company with guesses.
    reason: str | None = None
    # Search-budget accounting. `industry-research` documents that WebSearch is
    # capped per session and that a run which exhausts it degrades silently:
    # fetching keeps working so the output still looks complete, while discovery
    # has stopped. A company whose subagent ran out of budget is `degraded`, not
    # `done`, and gets re-queued.
    searches_used: int = 0
    budget_exhausted: bool = False
    tool_calls_used: int = 0

    @model_validator(mode="after")
    def _empty_needs_a_reason(self) -> "CandidateFile":
        if not self.candidates and not self.reason:
            raise ValueError(
                "a candidate file with no candidates must carry a `reason` explaining why. "
                "An unexplained empty file is indistinguishable from a crashed subagent."
            )
        return self

    @property
    def status(self) -> str:
        """How the account should be recorded after this run.

        `no_contacts` is deliberately distinct from `done`: a company that was
        researched properly and yielded nobody is not finished, it is waiting on
        something to change. Recording it as done hides it from every re-queue.
        """
        if self.budget_exhausted:
            return "degraded"
        return "done" if self.candidates else "no_contacts"


def validate_file(path: Path, suppressed: set[str] | None = None) -> CandidateFile:
    """Load and validate one candidate file. Raises CandidateError on anything wrong."""
    try:
        payload = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise CandidateError(f"{path}: not valid JSON\n  {exc}") from exc

    try:
        cf = CandidateFile.model_validate(payload)
    except ValidationError as exc:
        raise CandidateError(_format(exc, path)) from exc

    if suppressed:
        blocked = [c.email for c in cf.candidates if _is_suppressed(c, suppressed)]
        if blocked:
            raise CandidateError(
                f"{path}: {len(blocked)} candidate(s) are on the suppression list and must "
                f"not have been surfaced by discovery: {', '.join(blocked)}"
            )
    return cf


def filter_suppressed(cf: CandidateFile, suppressed: set[str]) -> tuple[CandidateFile, list[str]]:
    """Drop suppressed candidates rather than rejecting the whole file.

    Used on ingest, where a suppressed address showing up is expected drift
    rather than a discovery bug.
    """
    kept, dropped = [], []
    for c in cf.candidates:
        (dropped if _is_suppressed(c, suppressed) else kept).append(c)
    if dropped:
        cf = cf.model_copy(update={"candidates": kept})
    return cf, [c.email for c in dropped]


def _is_suppressed(c: Candidate, suppressed: set[str]) -> bool:
    return c.email in suppressed or c.domain in suppressed or c.company.lower() in suppressed


def _format(exc: ValidationError, path: Path) -> str:
    lines = [f"{path}: {len(exc.errors())} problem(s) in the candidate file."]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(record)"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def now() -> datetime:
    return datetime.now(timezone.utc)


JSON_SCHEMA_PATH = "references/schema.md"


def json_schema() -> dict[str, Any]:
    """Emitted into the research brief so the subagent writes against the real schema."""
    return CandidateFile.model_json_schema()


if __name__ == "__main__":  # python -m scripts.candidates > schema.json
    print(json.dumps(json_schema(), indent=2))
