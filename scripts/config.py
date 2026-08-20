"""Typed configuration. Every user-facing knob in the system lives here.

Rule 2 of the spec: nothing about a particular user appears in this file or any
other file under `scripts/`. These models describe the *shape* of a config
directory; the values come from `config/`, which is gitignored, with
`config.example/` as the shipped template.

Every model forbids extra keys so a typo fails at load with a suggestion.
"""

from __future__ import annotations

import math
import os
import re
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ConfigError, humanize

# Pydantic's EmailStr needs email-validator; keep the dependency surface small
# and validate with a deliberately conservative regex instead.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def valid_email(v: str) -> str:
    v = v.strip()
    if not EMAIL_RE.match(v):
        raise ValueError(f"not a valid email address: {v!r}")
    return v.lower()


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def wire_size(nbytes: int) -> int:
    """Base64-encoded size. What a receiving gateway actually measures."""
    return math.ceil(nbytes / 3) * 4


def human(nbytes: float) -> str:
    return f"{nbytes / 1_000_000:.2f} MB"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- persona


class PersonaProject(Strict):
    org: str
    blurb: str


class Persona(Strict):
    """Who the sender is. Rendered into every template; never hardcoded."""

    name: str
    first_name: str
    role: str
    org: str
    mailing_address: str
    links: dict[str, str] = Field(default_factory=dict)
    projects: list[PersonaProject] = Field(default_factory=list)
    unsubscribe_instructions: str
    body: str = ""

    @property
    def project_bullets(self) -> str:
        return "\n".join(f"- {p.org}: {p.blurb}" for p in self.projects)

    @property
    def link_lines(self) -> str:
        return "\n".join(f"{label}: {url}" for label, url in self.links.items())

    @property
    def signature(self) -> str:
        parts = [f"Sincerely,\n- {self.name}"]
        if self.links:
            parts.append(self.link_lines)
        return "\n".join(parts)

    @property
    def footer(self) -> str:
        """CAN-SPAM: opt-out mechanism plus a physical mailing address."""
        return f"{self.unsubscribe_instructions}\n\n{self.mailing_address}"


# ---------------------------------------------------------------- icp


class CompanySize(Strict):
    min: int = 1
    max: int = 100000


class ICP(Strict):
    titles: list[str] = Field(default_factory=list)
    title_excludes: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)
    company_size: CompanySize = Field(default_factory=CompanySize)
    include_regions: list[str] = Field(default_factory=list)
    # GDPR legitimate-interest for cold B2B is a deliberate call, not a default.
    exclude_regions: list[str] = Field(default_factory=lambda: ["EU", "UK", "EEA", "CH"])
    exclude_domains: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    max_contacts_per_company: int = 3
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# ---------------------------------------------------------------- campaign


class SendingWindow(Strict):
    days: list[str] = Field(default_factory=lambda: ["tue", "wed", "thu"])
    start: time = time(8, 0)
    end: time = time(16, 0)
    respect_recipient_timezone: bool = True

    @field_validator("days")
    @classmethod
    def _known_days(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d.lower() not in DAYS]
        if bad:
            raise ValueError(f"unknown day(s) {bad}; use {list(DAYS)}")
        return [d.lower() for d in v]

    @model_validator(mode="after")
    def _ordered(self) -> "SendingWindow":
        if self.start >= self.end:
            raise ValueError(f"start {self.start} must be before end {self.end}")
        return self


class InterSendDelay(Strict):
    distribution: Literal["uniform"] = "uniform"
    min_seconds: int = 90
    max_seconds: int = 420

    @model_validator(mode="after")
    def _ordered(self) -> "InterSendDelay":
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must be <= max_seconds")
        return self


class Warmup(Strict):
    enabled: bool = True
    start_per_day: int = 10
    increment_per_day: int = 5


class CircuitBreaker(Strict):
    """The single most important safety mechanism in the system."""

    enabled: bool = True
    window_sends: int = 200
    bounce_rate_threshold: float = Field(default=0.02, ge=0.0, le=1.0)


class Verification(Strict):
    enabled: bool = True
    # Chain order is meaningful: cheap checks first, paid API only as tiebreaker
    # on what the free path could not resolve.
    chain: list[Literal["mx", "smtp", "api"]] = Field(default_factory=lambda: ["mx", "smtp", "api"])
    api_provider: Literal["millionverifier", "zerobounce", "none"] = "none"
    api_key_env: str = "VERIFIER_API_KEY"
    smtp_timeout_seconds: int = 10
    smtp_probe_from: str = ""
    # PLACEHOLDER, not a rule. Frontier labs and AI startups are Workspace and
    # M365 nearly across the board, and both accept-all, so most contacts are
    # expected to land in catch_all. Revise from observed bounce rate once real
    # sends exist -- see references/deliverability.md.
    catch_all_daily_share: float = Field(default=0.20, ge=0.0, le=1.0)
    catch_all_share_is_placeholder: bool = True


class Discovery(Strict):
    subagent_tool_budget: int = 15
    companies_per_batch: int = 5
    cache_dir: str = "state/cache"
    candidates_dir: str = "state/candidates"


class Campaign(Strict):
    name: str
    test_recipient: str
    timezone: str = "America/Los_Angeles"
    daily_global_cap: int = 500
    sending_window: SendingWindow = Field(default_factory=SendingWindow)
    inter_send_delay: InterSendDelay = Field(default_factory=InterSendDelay)
    warmup: Warmup = Field(default_factory=Warmup)
    circuit_breaker: CircuitBreaker = Field(default_factory=CircuitBreaker)
    verification: Verification = Field(default_factory=Verification)
    discovery: Discovery = Field(default_factory=Discovery)
    attachments_root: str
    # Wire size, not disk size: base64 inflates by 4/3 and that is what a
    # gateway measures. Many corporate gateways reject inbound above 10 MB and
    # some above 5 MB, so an oversized attachment set hard-bounces for reasons
    # that have nothing to do with address quality -- contaminating bounce rate
    # and potentially tripping the circuit breaker on a false signal.
    # Two ceilings, deliberately independent.
    #
    # max_attachment_bytes is the hard load-time limit. It may be raised to let
    # a heavy set go out on *test* sends, which only ever reach an address you
    # control.
    #
    # campaign_max_attachment_bytes gates a campaign start and is checked
    # separately in preflight(), so a loosened test ceiling cannot leak into
    # real sending. Raising one does not raise the other.
    max_attachment_bytes: int = 5_000_000
    campaign_max_attachment_bytes: int = 5_000_000
    # Until a sending domain exists there is nowhere aligned to host the docs,
    # so step 1 ships attachments. The A/B starts at milestone 8.
    step1_variant: Literal["attachments", "links"] = "attachments"
    links_base_url: str | None = None

    @field_validator("test_recipient")
    @classmethod
    def _email(cls, v: str) -> str:
        return valid_email(v)

    @model_validator(mode="after")
    def _links_need_a_home(self) -> "Campaign":
        if self.step1_variant == "links" and not self.links_base_url:
            raise ValueError(
                "step1_variant is 'links' but links_base_url is unset. Host the "
                "documents on the sending domain and set links_base_url."
            )
        return self


# ---------------------------------------------------------------- mailboxes


class FromIdentity(Strict):
    name: str
    address: str

    @field_validator("address")
    @classmethod
    def _email(cls, v: str) -> str:
        return valid_email(v)

    def header(self) -> str:
        return f"{self.name} <{self.address}>"


class Mailbox(Strict):
    id: str
    provider: Literal["console", "gmail", "smtp"]
    from_: FromIdentity = Field(alias="from")
    # Cold sends go From the dedicated domain; replies land wherever the sender
    # actually reads mail.
    reply_to: str | None = None
    auth_ref: str | None = None
    daily_cap: int = 40
    warmup_start_date: date | None = None
    enabled: bool = True

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("reply_to")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        return valid_email(v) if v else None


class Mailboxes(Strict):
    mailboxes: list[Mailbox]

    @model_validator(mode="after")
    def _unique_ids(self) -> "Mailboxes":
        seen: set[str] = set()
        for m in self.mailboxes:
            if m.id in seen:
                raise ValueError(f"duplicate mailbox id {m.id!r}")
            seen.add(m.id)
        return self

    def enabled(self) -> list[Mailbox]:
        return [m for m in self.mailboxes if m.enabled]

    def get(self, mailbox_id: str) -> Mailbox:
        for m in self.mailboxes:
            if m.id == mailbox_id:
                return m
        raise ConfigError(f"no mailbox with id {mailbox_id!r} in mailboxes.yaml")


# ---------------------------------------------------------------- sequence


class AttachmentSet(Strict):
    dir: str
    files: list[str]


class Step(Strict):
    id: str
    template: str
    delay_business_days: int = 0
    jitter_business_days: int = 0
    attachment_set: str | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None


class Sequence(Strict):
    steps: list[Step]
    attachment_sets: dict[str, AttachmentSet] = Field(default_factory=dict)
    reply_templates: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "Sequence":
        if not self.steps:
            raise ValueError("sequence.yaml defines no steps")
        seen: set[str] = set()
        for s in self.steps:
            if s.id in seen:
                raise ValueError(f"duplicate step id {s.id!r}")
            seen.add(s.id)
            if s.attachment_set and s.attachment_set not in self.attachment_sets:
                raise ValueError(
                    f"step {s.id!r} references attachment_set {s.attachment_set!r}, "
                    f"which is not defined. Known: {sorted(self.attachment_sets)}"
                )
        return self

    def get(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise ConfigError(f"no step with id {step_id!r} in sequence.yaml")


# ---------------------------------------------------------------- cc


class CCRule(Strict):
    cc: list[str] | None = None
    bcc: list[str] | None = None


class CCConfig(Strict):
    default: CCRule = Field(default_factory=CCRule)
    by_step: dict[str, CCRule] = Field(default_factory=dict)
    by_campaign: dict[str, CCRule] = Field(default_factory=dict)
    by_domain: dict[str, CCRule] = Field(default_factory=dict)
    merge: bool = False


# ---------------------------------------------------------------- dorks


class Dork(Strict):
    id: str
    query: str
    signal: str
    enabled: bool = True


# ---------------------------------------------------------------- blackout


class BlackoutDates(Strict):
    dates: list[date] = Field(default_factory=list)
    ranges: list[dict[str, date]] = Field(default_factory=list)

    def covers(self, day: date) -> bool:
        if day in self.dates:
            return True
        for r in self.ranges:
            if r.get("start") and r.get("end") and r["start"] <= day <= r["end"]:
                return True
        return False


# ---------------------------------------------------------------- aggregate


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML\n  {exc}") from exc


def _load(model: type[BaseModel], path: Path, data: Any = None) -> Any:
    payload = _read_yaml(path) if data is None else data
    if not isinstance(payload, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level, got {type(payload).__name__}")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise humanize(exc, model, path) from exc


FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)

# A bracketed shout is how this project marks something a human still owes
# the config: [STREET ADDRESS NEEDED]. Structural, so it cannot be forgotten.
PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9 _/-]{3,}\]")


def load_persona(path: Path) -> Persona:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    raw = path.read_text()
    match = FRONTMATTER.match(raw)
    if not match:
        raise ConfigError(
            f"{path}: expected YAML frontmatter delimited by --- at the top of the file"
        )
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML frontmatter\n  {exc}") from exc
    data["body"] = match.group(2).strip()
    try:
        return Persona.model_validate(data)
    except ValidationError as exc:
        raise humanize(exc, Persona, path) from exc


class Config:
    """Everything under `config/`, loaded and validated as one unit."""

    def __init__(self, root: Path):
        self.root = Path(root)
        if not self.root.exists():
            raise ConfigError(
                f"config directory not found: {self.root}\n"
                f"  Copy config.example/ to config/ and fill it in."
            )
        self.persona = load_persona(self.root / "persona.md")
        self.icp: ICP = _load(ICP, self.root / "icp.yaml")
        self.campaign: Campaign = _load(Campaign, self.root / "campaign.yaml")
        self.mailboxes: Mailboxes = _load(Mailboxes, self.root / "mailboxes.yaml")
        self.sequence: Sequence = _load(Sequence, self.root / "sequence.yaml")
        self.cc: CCConfig = _load(CCConfig, self.root / "cc.yaml")
        self.blackout: BlackoutDates = _load(BlackoutDates, self.root / "blackout_dates.yaml")
        self.dorks: list[Dork] = self._load_dorks()
        self.templates_dir = self.root / "templates"
        self._cross_check()

    def _load_dorks(self) -> list[Dork]:
        path = self.root / "dorks.yaml"
        payload = _read_yaml(path)
        if isinstance(payload, dict):
            payload = payload.get("dorks", [])
        if not isinstance(payload, list):
            raise ConfigError(f"{path}: expected a list of search seeds")
        out = []
        for item in payload:
            try:
                out.append(Dork.model_validate(item))
            except ValidationError as exc:
                raise humanize(exc, Dork, path) from exc
        return out

    def _cross_check(self) -> None:
        """Catch the cross-file mistakes single-model validation cannot see."""
        problems: list[str] = []

        for step in self.sequence.steps:
            tpl = self.templates_dir / step.template
            if not tpl.exists():
                problems.append(f"sequence.yaml step {step.id!r} -> missing template {tpl}")

        root = Path(self.campaign.attachments_root).expanduser()
        limit = self.campaign.max_attachment_bytes
        for name, aset in self.sequence.attachment_sets.items():
            sizes: list[tuple[str, int]] = []
            for fname in aset.files:
                fpath = root / aset.dir / fname
                if not fpath.exists():
                    problems.append(f"attachment set {name!r} -> missing file {fpath}")
                else:
                    sizes.append((fname, fpath.stat().st_size))

            if not sizes:
                continue
            total_wire = wire_size(sum(n for _, n in sizes))
            if total_wire > limit:
                lines = [
                    f"attachment set {name!r} is {human(total_wire)} on the wire, over the "
                    f"{human(limit)} max_attachment_bytes limit. Many corporate gateways "
                    f"reject inbound above 10 MB and some above 5 MB, so this set would "
                    f"hard-bounce for reasons unrelated to address quality."
                ]
                for fname, nbytes in sorted(sizes, key=lambda x: -x[1]):
                    share = 100 * nbytes / sum(n for _, n in sizes)
                    lines.append(
                        f"      {human(wire_size(nbytes)):>9} wire  {share:5.1f}%  {fname}"
                    )
                biggest = max(sizes, key=lambda x: x[1])
                remainder = wire_size(sum(n for f, n in sizes if f != biggest[0]))
                lines.append(
                    f"      dropping {biggest[0]} leaves {human(remainder)}"
                )
                problems.append("\n    ".join(lines))

        if not self.mailboxes.enabled():
            problems.append("mailboxes.yaml: no mailbox has enabled: true")

        if problems:
            raise ConfigError(
                f"{self.root}: {len(problems)} cross-file problem(s).\n"
                + "\n".join(f"  {p}" for p in problems)
            )

    def preflight(self, mode: str = "campaign") -> list[str]:
        """Things that are structurally valid but must not reach a stranger.

        Separate from load-time validation on purpose: a test send to yourself
        should still render a placeholder so you can see it, while a campaign
        must refuse to start until a human has filled it in.
        """
        blockers: list[str] = []

        for field, value in (
            ("persona.mailing_address", self.persona.mailing_address),
            ("persona.unsubscribe_instructions", self.persona.unsubscribe_instructions),
        ):
            for hit in PLACEHOLDER_RE.findall(value or ""):
                blockers.append(
                    f"{field} still contains the placeholder {hit}. CAN-SPAM requires a "
                    f"real physical mailing address in every commercial solicitation, and "
                    f"the footer is appended to every template automatically."
                )
        for label, url in self.persona.links.items():
            if PLACEHOLDER_RE.search(url):
                blockers.append(f"persona.links[{label!r}] is still a placeholder: {url}")

        for mb in self.mailboxes.enabled():
            if PLACEHOLDER_RE.search(mb.from_.address) or "TBD" in mb.from_.address.upper():
                blockers.append(
                    f"mailbox {mb.id!r} is enabled but its from address is a placeholder: "
                    f"{mb.from_.address}"
                )

        if mode == "campaign":
            limit = self.campaign.campaign_max_attachment_bytes
            root = Path(self.campaign.attachments_root).expanduser()
            for name, aset in self.sequence.attachment_sets.items():
                total = wire_size(sum(
                    (root / aset.dir / f).stat().st_size
                    for f in aset.files
                    if (root / aset.dir / f).exists()
                ))
                if total > limit:
                    blockers.append(
                        f"attachment set {name!r} is {human(total)} on the wire, over the "
                        f"{human(limit)} campaign_max_attachment_bytes gate. "
                        f"max_attachment_bytes may be higher to allow test sends, but a "
                        f"campaign must not ship a set this size to strangers."
                    )

            if self.campaign.step1_variant == "links" and not self.campaign.links_base_url:
                blockers.append("step1_variant is 'links' but links_base_url is unset")

        return blockers

    def secrets(self) -> dict[str, str]:
        """Read secrets.env if present. Never logged, never committed."""
        path = self.root / "secrets.env"
        out: dict[str, str] = {}
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out

    def secret(self, key: str, default: str | None = None) -> str | None:
        return self.secrets().get(key) or os.environ.get(key) or default


def default_config_root() -> Path:
    env = os.environ.get("OUTBOUND_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "config"


def load_config(root: Path | str | None = None) -> Config:
    return Config(Path(root).expanduser() if root else default_config_root())
