"""Template rendering. Deterministic string substitution, no model in the loop.

A template file is YAML frontmatter plus a body:

    ---
    subject: Would it be possible to collaborate with your team at {{ account.name }}?
    ---
    Hello {{ contact.first_name }}!
    ...

Undefined variables raise rather than rendering empty, so a typo in a template
fails at render time instead of mailing a stranger a blank line.

`personalization` is the one variable allowed to be null. Templates must guard
it, and the fallback is the correct outcome whenever discovery could not ground
a detail about someone's work.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined, TemplateError

from .config import Campaign, Config, Persona, Step
from .errors import ConfigError

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def _env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)
    env.filters["title_case"] = lambda s: str(s).title()
    return env


@dataclass
class Attachment:
    path: Path
    name: str

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


@dataclass
class RenderedEmail:
    subject: str
    body: str
    to: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    from_header: str = ""
    reply_to: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    variant: str = "attachments"
    template_hash: str = ""
    step_id: str = ""
    campaign: str | None = None

    @property
    def recipient_count(self) -> int:
        """What the provider's daily recipient cap actually counts."""
        return 1 + len(self.cc) + len(self.bcc)

    @property
    def body_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()[:16]

    @property
    def attachment_bytes(self) -> int:
        return sum(a.size for a in self.attachments)

    def preview(self) -> str:
        lines = [
            f"From:       {self.from_header}",
            f"To:         {self.to}",
            f"Cc:         {', '.join(self.cc) or '(none)'}",
            f"Bcc:        {', '.join(self.bcc) or '(none)'}",
        ]
        if self.reply_to:
            lines.append(f"Reply-To:   {self.reply_to}")
        lines += [
            f"Subject:    {self.subject}",
            f"Variant:    {self.variant}",
        ]
        if self.attachments:
            total_mb = self.attachment_bytes / 1_048_576
            names = ", ".join(f"{a.name} ({a.size/1_048_576:.1f} MB)" for a in self.attachments)
            lines.append(f"Attach:     {names}")
            lines.append(f"Attach tot: {total_mb:.1f} MB")
        return "\n".join(lines) + "\n" + "-" * 72 + "\n" + self.body


def parse_template(path: Path) -> tuple[str, str]:
    """Return (subject, body_template) for a template file."""
    if not path.exists():
        raise ConfigError(f"missing template: {path}")
    raw = path.read_text()
    match = FRONTMATTER.match(raw)
    if not match:
        raise ConfigError(
            f"{path}: template needs YAML frontmatter with a `subject:` key.\n"
            f"  ---\n  subject: ...\n  ---\n  <body>"
        )
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid frontmatter\n  {exc}") from exc
    subject = meta.get("subject")
    if not subject:
        raise ConfigError(f"{path}: frontmatter has no `subject:` key")
    unknown = set(meta) - {"subject"}
    if unknown:
        raise ConfigError(f"{path}: unknown frontmatter key(s): {', '.join(sorted(unknown))}")
    return str(subject), match.group(2)


def template_hash(config: Config, campaign: str | None = None) -> str:
    """Fingerprint every template plus the persona that gets injected into them.

    The scheduler refuses to start a campaign whose hash differs from the one on
    the last passing test send, so this must cover anything that changes what a
    recipient sees.
    """
    h = hashlib.sha256()
    h.update((campaign or "").encode())
    for step in config.steps_for(campaign):
        path = config.template_path(step, campaign)
        h.update(step.id.encode())
        h.update(path.read_bytes() if path.exists() else b"<missing>")
    for name in sorted(config.sequence.reply_templates.values()):
        path = config.templates_dir_for(campaign) / name
        if not path.exists():
            path = config.templates_dir / name
        if path.exists():
            h.update(path.read_bytes())
    h.update((config.root / "persona.md").read_bytes())
    h.update(config.campaign.step1_variant.encode())
    return h.hexdigest()[:16]


def attachments_for(config: Config, step: Step) -> list[Attachment]:
    """Resolve a named attachment set to real files under attachments_root."""
    if not step.attachment_set:
        return []
    aset = config.sequence.attachment_sets[step.attachment_set]
    root = Path(config.campaign.attachments_root).expanduser()
    out = []
    for fname in aset.files:
        path = root / aset.dir / fname
        if not path.exists():
            raise ConfigError(f"attachment set {step.attachment_set!r}: missing file {path}")
        out.append(Attachment(path=path, name=fname))
    return out


def document_links(config: Config, step: Step) -> list[tuple[str, str]]:
    """The links variant of an attachment set, hosted on the sending domain."""
    if not step.attachment_set or not config.campaign.links_base_url:
        return []
    aset = config.sequence.attachment_sets[step.attachment_set]
    base = config.campaign.links_base_url.rstrip("/")
    return [(f, f"{base}/{aset.dir.strip('/')}/{f}") for f in aset.files]


def build_context(
    *,
    persona: Persona,
    campaign: Campaign,
    contact: dict[str, Any],
    account: dict[str, Any],
    personalization: str | None,
    links: list[tuple[str, str]],
    campaign_name: str | None = None,
) -> dict[str, Any]:
    return {
        "contact": contact,
        "account": account,
        "personalization": personalization,
        "persona": {
            "name": persona.name,
            "first_name": persona.first_name,
            "role": persona.role,
            "org": persona.org,
            "links": persona.links,
            "link_lines": persona.link_lines,
            "project_bullets": persona.project_bullets,
            "signature": persona.signature,
            "mailing_address": persona.mailing_address,
            "footer": persona.footer,
            "unsubscribe_instructions": persona.unsubscribe_instructions,
        },
        "campaign": {"name": campaign_name or campaign.name,
                     "variant": campaign.step1_variant},
        "document_links": [{"name": n, "url": u} for n, u in links],
    }


def render(
    config: Config,
    step: Step,
    *,
    contact: dict[str, Any],
    account: dict[str, Any],
    to: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    from_header: str = "",
    reply_to: str | None = None,
    campaign: str | None = None,
) -> RenderedEmail:
    """Render one email. Pure function of config + contact; no I/O beyond files."""
    subject_tpl, body_tpl = parse_template(config.template_path(step, campaign))

    # Step 1 has two variants and the A/B settles which performs better. Later
    # steps always attach, because by then the recipient has engaged.
    is_first = step.id == config.steps_for(campaign)[0].id
    variant = config.campaign.step1_variant if is_first else "attachments"
    if variant == "links" and is_first:
        attachments: list[Attachment] = []
        links = document_links(config, step)
    else:
        attachments = attachments_for(config, step)
        links = []

    ctx = build_context(
        persona=config.persona,
        campaign=config.campaign,
        campaign_name=campaign,
        contact=contact,
        account=account,
        personalization=contact.get("personalization"),
        links=links,
    )

    env = _env()
    try:
        subject = env.from_string(subject_tpl).render(**ctx).strip()
        body = env.from_string(body_tpl).render(**ctx)
    except TemplateError as exc:
        raise ConfigError(f"{step.template}: render failed -- {exc}") from exc

    body = append_footer(body, config.persona)

    return RenderedEmail(
        subject=subject,
        body=body,
        to=to,
        cc=list(cc or []),
        bcc=list(bcc or []),
        from_header=from_header,
        reply_to=reply_to,
        attachments=attachments,
        variant=variant,
        template_hash=template_hash(config, campaign),
        step_id=step.id,
        campaign=campaign,
    )


FOOTER_SEP = "\n\n-- \n"


def append_footer(body: str, persona: Persona) -> str:
    """CAN-SPAM: every outbound email carries opt-out and a mailing address.

    Appended here rather than left to the template so it cannot be forgotten in
    one template out of four.
    """
    if FOOTER_SEP.strip() in body and persona.unsubscribe_instructions in body:
        return body
    return body.rstrip("\n") + FOOTER_SEP + persona.footer + "\n"
