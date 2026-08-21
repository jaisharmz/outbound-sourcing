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

from .config import Campaign, Config, Persona, Step, wire_size
from .errors import ConfigError

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def _env() -> Environment:
    # trim_blocks/lstrip_blocks so a {% if %} guard does not leave a blank line
    # behind when it is false. Without them, an absent personalization or an
    # empty link list shows up as a gap in the middle of the email.
    env = Environment(undefined=StrictUndefined, autoescape=False,
                      keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)
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
    body: str                    # the plain-text alternative
    to: str
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    from_header: str = ""
    reply_to: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    template_hash: str = ""
    step_id: str = ""
    campaign: str | None = None
    body_html: str = ""          # empty when the template is plain text

    @property
    def is_html(self) -> bool:
        return bool(self.body_html.strip())

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
    for step in config.steps_for(campaign):
        for label, url in sorted(step.links.items()):
            h.update(f"{label}={url}".encode())
    h.update((config.root / "persona.md").read_bytes())
    return h.hexdigest()[:16]


def resolve_documents(config: Config, step: Step) -> tuple[list[Attachment], list[tuple[str, str]]]:
    """Split a step's documents into attachments and links, by size.

    Attach everything that fits under max_attachment_bytes on the wire; link the
    rest. Smallest first, so one oversized document does not push out two small
    ones. A document with a local file always prefers attaching -- which means
    compressing it later moves it across the line with no config change.
    """
    if not step.attachment_set:
        return [], list(step.links.items())
    aset = config.sequence.attachment_sets[step.attachment_set]
    root = Path(config.campaign.attachments_root).expanduser()
    # The stricter of the two ceilings decides the split. Using the looser
    # test-send ceiling produces a set that attaches everything and then fails
    # the campaign gate, which is the wrong shape of failure: the rule should
    # produce a sendable message, not an unsendable one plus an error.
    cap = min(config.campaign.max_attachment_bytes,
              config.campaign.campaign_max_attachment_bytes)

    sized = []
    for doc in aset.documents:
        path = (root / aset.dir / doc.file) if doc.file else None
        size = path.stat().st_size if path and path.exists() else None
        sized.append((doc, path if size is not None else None, size))
    sized.sort(key=lambda t: (t[2] is None, t[2] or 0))

    attachments: list[Attachment] = []
    links: list[tuple[str, str]] = []
    running = 0
    for doc, path, size in sized:
        if path is not None and size is not None:
            if wire_size(running + size) <= cap:
                running += size
                attachments.append(Attachment(path=path, name=path.name))
                continue
            if not doc.url:
                which = ("campaign_max_attachment_bytes"
                         if cap == config.campaign.campaign_max_attachment_bytes
                         else "max_attachment_bytes")
                raise ConfigError(
                    f"document {doc.name!r} does not fit and has no url to fall back to. "
                    f"It is {wire_size(size)/1_000_000:.2f} MB on the wire; "
                    f"{wire_size(running)/1_000_000:.2f} MB is already attached against a "
                    f"{cap/1_000_000:.2f} MB ceiling ({which}). Add a url, or compress it."
                )
        if doc.url:
            links.append((doc.name, doc.url))
        else:
            raise ConfigError(f"document {doc.name!r}: file not found and no url given")
    links.extend(step.links.items())
    return attachments, links


def attachments_for(config: Config, step: Step) -> list[Attachment]:
    return resolve_documents(config, step)[0]


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
            # The structured list, so an HTML template can mark each project up
            # itself instead of receiving a pre-joined plain-text block.
            "projects": [{"org": x.org, "blurb": x.blurb} for x in persona.projects],
        },
        "campaign": {"name": campaign_name or campaign.name},
        "document_links": [{"name": n, "url": u} for n, u in links],
    }


_TAG = re.compile(r"<[^>]+>")
_ANCHOR = re.compile(r'<a\b[^>]*>(.*?)</a>', re.I | re.S)
# Swallow the newline the template itself puts after <br>, or every
# single line break becomes a blank line in the text part.
_BR = re.compile(r"<br\s*/?>[ \t]*\n?", re.I)
_BLOCK_END = re.compile(r"</(p|div|ul|ol|li|h[1-6])>", re.I)
HTML_START = re.compile(r"^\s*<(p|div|table|html|h[1-6])\b", re.I)


def looks_like_html(body: str) -> bool:
    return bool(HTML_START.match(body))


def html_to_text(html: str) -> str:
    """The plain-text alternative, for clients that will not render HTML.

    It has to degrade cleanly, which means it must not look like a failed
    attempt at formatting. Two rules follow:

      Bold becomes nothing. `<strong>x</strong>` renders as `x`, never `**x**`.
      Asterisks in a text/plain part are the visible residue of markdown that
      did not run, and they read as a broken mail merge.

      A link becomes its anchor text alone: `Google Scholar profile`, not the
      URL and not `text (url)`. A bare URL in running prose is the other half of
      the same tell. The cost is real and worth stating plainly: a text-only
      reader cannot follow the link, and reaches the signature knowing a profile
      exists with no way to open it.
    """
    text = _ANCHOR.sub(lambda m: _TAG.sub("", m.group(1)), html)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n\n", text)
    text = _TAG.sub("", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    out, blanks = [], 0
    for line in text.splitlines():
        if line.strip():
            out.append(line.strip())
            blanks = 0
        else:
            blanks += 1
            if blanks == 1 and out:
                out.append("")
    return "\n".join(out).strip() + "\n"


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

    # Attach what fits, link what does not. The split is computed from real file
    # sizes rather than declared per document.
    attachments, links = resolve_documents(config, step)

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

    # An HTML template carries its own plain-text alternative, derived rather
    # than written twice -- two hand-maintained bodies drift, and the one nobody
    # looks at is the one that ships broken.
    # A document that resolve_documents decided to link, in a template with no
    # place to put it, would vanish silently. That is how the portfolio would
    # stop being sent without anything reporting it.
    if links and "document_links" not in body_tpl:
        raise ConfigError(
            f"{step.template} renders no document_links block, but "
            f"{len(links)} document(s) resolved to links rather than attachments: "
            f"{', '.join(n for n, _ in links)}. They would be dropped silently. "
            f"Either add a document_links block to the template, or remove the "
            f"document from the attachment set so the omission is deliberate."
        )

    html = body if looks_like_html(body) else ""
    if html:
        body = html_to_text(html)

    return RenderedEmail(
        subject=subject,
        body=body,
        body_html=html,
        to=to,
        cc=list(cc or []),
        bcc=list(bcc or []),
        from_header=from_header,
        reply_to=reply_to,
        attachments=attachments,
        template_hash=template_hash(config, campaign),
        step_id=step.id,
        campaign=campaign,
    )


# Nothing is appended to a rendered body. What the template says is what sends.
# These are personal emails proposing research collaboration, and an auto-generated
# footer makes them read like a mail merge -- see references/compliance.md for why
# the opt-out obligation is met by honoring requests rather than advertising them.
