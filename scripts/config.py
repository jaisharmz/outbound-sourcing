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
    links: dict[str, str] = Field(default_factory=dict)
    projects: list[PersonaProject] = Field(default_factory=list)
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
    # Applied to the reason a source run gave for setting a company aside. These
    # are the kinds that are never targets whatever the campaign; everything else
    # goes to triage rather than being dropped.
    auto_drop_reason_patterns: list[str] = Field(default_factory=list)
    max_contacts_per_company: int = 3
    # Traversal surfaces a research group together, and four people from one lab
    # will compare notes. Capped separately from company.
    max_contacts_per_lab: int = 2
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# ---------------------------------------------------------------- campaign


class SendingWindow(Strict):
    days: list[str] = Field(default_factory=lambda: ["tue", "wed", "thu"])
    start: time = time(8, 0)
    end: time = time(16, 0)

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


class Verification(Strict):
    """MX lookup then an SMTP RCPT probe. No paid tier.

    Most Workspace and M365 domains are accept-all, so `catch_all` is the
    expected outcome rather than an exception, and it sends normally. At this
    volume the review gate is what stands between an inferred-pattern address
    and a send, which is why the review export shows the basis as its own column.
    """

    enabled: bool = True
    chain: list[Literal["mx", "smtp"]] = Field(default_factory=lambda: ["mx", "smtp"])
    smtp_timeout_seconds: int = 10
    smtp_probe_from: str = ""


class Discovery(Strict):
    subagent_tool_budget: int = 15
    companies_per_batch: int = 5
    cache_dir: str = "state/cache"
    candidates_dir: str = "state/candidates"


class Campaign(Strict):
    name: str
    test_recipient: str
    # --to is the only path in the system that reaches an arbitrary address
    # without passing the review gate. Entries are exact addresses or *@domain.
    # test_recipient is always allowed implicitly.
    test_send_allowlist: list[str] = Field(default_factory=list)
    timezone: str = "America/Los_Angeles"
    # Safety rail on a manually invoked send, not a scheduler budget.
    daily_cap: int = 25
    sending_window: SendingWindow = Field(default_factory=SendingWindow)
    inter_send_delay: InterSendDelay = Field(default_factory=InterSendDelay)
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

    @field_validator("test_recipient")
    @classmethod
    def _email(cls, v: str) -> str:
        return valid_email(v)

    @field_validator("test_send_allowlist")
    @classmethod
    def _allowlist_entries(cls, v: list[str]) -> list[str]:
        out = []
        for entry in v:
            e = entry.strip().lower()
            if e.startswith("*@"):
                if "." not in e[2:]:
                    raise ValueError(f"wildcard entry {entry!r} needs a real domain after *@")
                out.append(e)
            elif EMAIL_RE.match(e):
                out.append(e)
            else:
                raise ValueError(
                    f"test_send_allowlist entry {entry!r} is neither an email address nor "
                    f"a *@domain wildcard"
                )
        return out

    def allows_test_recipient(self, address: str) -> bool:
        addr = address.strip().lower()
        if addr == self.test_recipient:
            return True
        domain = addr.partition("@")[2]
        for entry in self.test_send_allowlist:
            if entry.startswith("*@"):
                # Subdomains count: mail-tester hands out @srv1.mail-tester.com.
                suffix = entry[2:]
                if domain == suffix or domain.endswith("." + suffix):
                    return True
            elif entry == addr:
                return True
        return False



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
    enabled: bool = True
    # SMTP/IMAP settings. Ignored by other providers. `username` defaults to the
    # from address; auth_ref names the secrets.env key holding the password.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    # Where create_draft APPENDs. Gmail localises this folder, so a non-English
    # account needs it set explicitly or the APPEND fails with "no such mailbox".
    drafts_folder: str = "[Gmail]/Drafts"
    username: str | None = None

    @property
    def login(self) -> str:
        return self.username or self.from_.address

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


DRIVE_VIEW_RE = re.compile(r"drive\.google\.com/file/d/([\w-]+)")


class Document(Strict):
    """One document, which may exist locally, remotely, or both.

    The rule, encoded rather than decided per document: attach anything that
    keeps the message under max_attachment_bytes, link anything that does not.
    Where both a local file and a URL exist, attaching wins and the link is the
    fallback -- so compressing a file later turns it into an attachment with no
    config change.
    """

    name: str
    file: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _has_a_source(self) -> "Document":
        if not self.file and not self.url:
            raise ValueError(f"document {self.name!r} has neither a file nor a url")
        if self.url and DRIVE_VIEW_RE.search(self.url):
            fid = DRIVE_VIEW_RE.search(self.url).group(1)
            raise ValueError(
                f"document {self.name!r} links to the Drive viewer, which wraps the file in "
                f"Drive chrome and asks some recipients to sign in. Use the direct-download "
                f"form: https://drive.google.com/uc?export=download&id={fid}"
            )
        if self.url and not self.url.startswith("http"):
            raise ValueError(f"document {self.name!r}: url must be absolute")
        return self


class AttachmentSet(Strict):
    dir: str = ""
    documents: list[Document] = Field(default_factory=list)


class Step(Strict):
    id: str
    template: str
    delay_business_days: int = 0
    jitter_business_days: int = 0
    attachment_set: str | None = None
    # Documents linked rather than attached, by name. Independent of the
    # attachment set: a first touch can attach two files and link a third.
    links: dict[str, str] = Field(default_factory=dict)
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


class CampaignDef(Strict):
    """One named campaign: which accounts it targets and what copy it uses.

    Two campaigns targeting different tiers fail differently and cannot share
    copy. A startup ignores you because nobody read it; a frontier lab ignores
    you because the named researcher has no mechanism to engage an outside group
    without a formal partnership process. A blended reply rate hides which one
    is working, so they stay separate all the way through reporting.
    """

    description: str = ""
    # Which landscape tiers enroll into this campaign.
    tiers: list[str] = Field(default_factory=list)
    # Which stage-0 depths enroll here. A company that trains its own models and
    # one that ships features on somebody else's need different copy, so depth
    # routes a campaign the same way tier does.
    ai_depth: list[str] = Field(default_factory=list)
    # Relative to config/. Falls back to templates/ when unset.
    templates_dir: str | None = None
    # Falls back to the steps in sequence.yaml when unset.
    steps: list["Step"] | None = None


class Campaigns(Strict):
    campaigns: dict[str, CampaignDef] = Field(default_factory=dict)

    def for_tier(self, tier: str) -> str | None:
        for name, c in self.campaigns.items():
            if tier in c.tiers:
                return name
        return None

    def for_depth(self, depth: str | None) -> str | None:
        if not depth:
            return None
        for name, c in self.campaigns.items():
            if depth in c.ai_depth:
                return name
        return None

    def depth_routes(self) -> dict[str, str]:
        return {d: name for name, c in self.campaigns.items() for d in c.ai_depth}

    def get(self, name: str) -> CampaignDef:
        if name not in self.campaigns:
            raise ConfigError(
                f"no campaign named {name!r} in campaigns.yaml. "
                f"Known: {sorted(self.campaigns) or '(none)'}"
            )
        return self.campaigns[name]


class ExcludedLab(Strict):
    name: str
    institution: str | None = None
    reason: str = ""
    aliases: list[str] = Field(default_factory=list)

    def matches(self, text: str) -> bool:
        t = (text or "").lower()
        for token in [self.name, *self.aliases]:
            if token and token.lower() in t:
                return True
        return False


class PersonalExclusions(Strict):
    """People the operator already knows. Checked at discovery, not at send."""

    labs: list[ExcludedLab] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)

    def excluded_lab(self, text: str) -> ExcludedLab | None:
        for lab in self.labs:
            if lab.matches(text):
                return lab
        return None

    def excluded_person(self, name: str) -> bool:
        n = (name or "").strip().lower()
        return any(p.strip().lower() == n for p in self.people)


class FundSpec(BaseModel):
    """How to read one fund's portfolio page. Keys vary by strategy."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    url: str
    strategy: Literal["embedded_json", "list_plus_detail", "sitemap_names"]


class Funds(Strict):
    funds: dict[str, FundSpec] = Field(default_factory=dict)


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
        self.personal_exclusions: PersonalExclusions = (
            _load(PersonalExclusions, self.root / "personal_exclusions.yaml")
            if (self.root / "personal_exclusions.yaml").exists()
            else PersonalExclusions()
        )
        self.funds: Funds = (
            _load(Funds, self.root / "funds.yaml")
            if (self.root / "funds.yaml").exists()
            else Funds()
        )
        self.campaigns: Campaigns = (
            _load(Campaigns, self.root / "campaigns.yaml")
            if (self.root / "campaigns.yaml").exists()
            else Campaigns()
        )
        self.blackout: BlackoutDates = _load(BlackoutDates, self.root / "blackout_dates.yaml")
        self.dorks: list[Dork] = self._load_dorks()
        self.templates_dir = self.root / "templates"
        self._cross_check()

    # ------------------------------------------------ per-campaign resolution

    def steps_for(self, campaign: str | None = None) -> list[Step]:
        """The sequence a campaign runs. Falls back to sequence.yaml."""
        if campaign and campaign in self.campaigns.campaigns:
            steps = self.campaigns.campaigns[campaign].steps
            if steps:
                return steps
        return self.sequence.steps

    def step_for(self, step_id: str, campaign: str | None = None) -> Step:
        for s in self.steps_for(campaign):
            if s.id == step_id:
                return s
        raise ConfigError(
            f"no step {step_id!r} in campaign {campaign or '(default)'}. "
            f"Known: {[s.id for s in self.steps_for(campaign)]}"
        )

    def templates_dir_for(self, campaign: str | None = None) -> Path:
        if campaign and campaign in self.campaigns.campaigns:
            sub = self.campaigns.campaigns[campaign].templates_dir
            if sub:
                return self.root / sub
        return self.templates_dir

    def template_path(self, step: Step, campaign: str | None = None) -> Path:
        """Campaign-specific template if it exists, otherwise the shared one."""
        candidate = self.templates_dir_for(campaign) / step.template
        return candidate if candidate.exists() else self.templates_dir / step.template

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

        for name in self.campaigns.campaigns:
            for step in self.steps_for(name):
                if not self.template_path(step, name).exists():
                    problems.append(
                        f"campaign {name!r} step {step.id!r} -> no template at "
                        f"{self.templates_dir_for(name) / step.template} or {self.templates_dir}"
                    )

        # Oversize is not a load-time error: a document that does not fit becomes
        # a link (see templates.resolve_documents). The only failure here is a
        # document with neither a readable file nor a url -- nothing to send.
        root = Path(self.campaign.attachments_root).expanduser()
        for name, aset in self.sequence.attachment_sets.items():
            for doc in aset.documents:
                if doc.file and not (root / aset.dir / doc.file).exists() and not doc.url:
                    problems.append(
                        f"attachment set {name!r}: {doc.file} is missing and "
                        f"{doc.name!r} has no url to fall back to"
                    )

        if not self.mailboxes.enabled():
            problems.append("mailboxes.yaml: no mailbox has enabled: true")

        if problems:
            raise ConfigError(
                f"{self.root}: {len(problems)} cross-file problem(s).\n"
                + "\n".join(f"  {p}" for p in problems)
            )

    def preflight(self, mode: str = "campaign", campaign: str | None = None) -> list[str]:
        """Things that are structurally valid but must not reach a stranger.

        Separate from load-time validation on purpose: a test send to yourself
        should still render a placeholder so you can see it, while a campaign
        must refuse to start until a human has filled it in.
        """
        blockers: list[str] = []

        for label, url in self.persona.links.items():
            if PLACEHOLDER_RE.search(url):
                blockers.append(f"persona.links[{label!r}] is still a placeholder: {url}")

        # A stub blocks its own campaign, not every campaign. applied-ai is
        # permanently blocked by design, and without this scoping it would
        # permanently block the campaign that is actually ready to send.
        scope = [campaign] if campaign else list(self.campaigns.campaigns)
        if mode != "campaign":
            scope = []

        for name in scope:
            for step in self.steps_for(name):
                path = self.template_path(step, name)
                if not path.exists():
                    continue
                for hit in PLACEHOLDER_RE.findall(path.read_text()):
                    blockers.append(
                        f"campaign {name!r} template {step.template} is still a stub: "
                        f"contains {hit}. Write the copy before this campaign can start."
                    )

        # A linked document with no URL is an unwritten email by another route.
        for name in scope:
            for step in self.steps_for(name):
                for label, url in step.links.items():
                    for hit in PLACEHOLDER_RE.findall(url):
                        blockers.append(
                            f"campaign {name!r} step {step.id!r} links {label!r} to the "
                            f"placeholder {hit}. Set the real URL before this campaign "
                            f"can start."
                        )

        for mb in self.mailboxes.enabled():
            if PLACEHOLDER_RE.search(mb.from_.address) or "TBD" in mb.from_.address.upper():
                blockers.append(
                    f"mailbox {mb.id!r} is enabled but its from address is a placeholder: "
                    f"{mb.from_.address}"
                )

        if mode == "campaign":
            # What will actually be attached, after the size split -- not the
            # whole set, since oversized documents become links.
            from .templates import resolve_documents
            limit = self.campaign.campaign_max_attachment_bytes
            for step in self.steps_for(campaign):
                try:
                    attachments, _links = resolve_documents(self, step)
                except ConfigError as exc:
                    blockers.append(str(exc))
                    continue
                total = wire_size(sum(a.size for a in attachments))
                if total > limit:
                    blockers.append(
                        f"step {step.id!r} would attach {human(total)} on the wire, over the "
                        f"{human(limit)} campaign_max_attachment_bytes gate."
                    )

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
