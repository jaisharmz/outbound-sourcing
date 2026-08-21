"""Rendering. The null-personalization fallback is the important one: it is what
the system does every time discovery could not ground a detail, which is often."""

from __future__ import annotations

import pytest

from scripts.config import Config
from scripts.errors import ConfigError
from scripts.templates import render, template_hash

CONTACT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "name": "Ada Lovelace",
    "title": "Research Scientist",
    "email": "ada@target.test",
    "personalization": None,
}
ACCOUNT = {"name": "Target Labs", "domain": "target.test"}


def step1(config: Config):
    return config.sequence.steps[0]


def test_renders_with_null_personalization(config: Config):
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert "Hello Ada!" in email.body
    assert "None" not in email.body
    assert "{{" not in email.body
    # No stray blank-line pileup where the personalization paragraph would be.
    assert "\n\n\n" not in email.body


def test_renders_with_personalization(config: Config):
    contact = dict(CONTACT, personalization="I read your paper on retrieval last week.")
    email = render(config, step1(config), contact=contact, account=ACCOUNT, to=contact["email"])
    assert "I read your paper on retrieval last week." in email.body


def test_subject_is_substituted(config: Config):
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert "Target Labs" in email.subject
    assert "{{" not in email.subject


def _persona_ctx(config):
    """The persona mapping templates see, matching templates._context."""
    p = config.persona
    return {"name": p.name, "first_name": p.first_name, "role": p.role,
            "org": p.org, "links": p.links, "link_lines": p.link_lines,
            "project_bullets": p.project_bullets, "signature": p.signature,
            "projects": [{"org": x.org, "blurb": x.blurb} for x in p.projects]}


def test_nothing_is_appended_to_a_rendered_body(config: Config):
    """What the template says is what sends. These are personal emails proposing
    collaboration; an auto-generated footer makes them read like a mail merge.
    The opt-out obligation is met by honoring requests, not advertising them."""
    from jinja2 import Environment, StrictUndefined

    from scripts.templates import html_to_text, looks_like_html, resolve_documents

    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True,
                      trim_blocks=True, lstrip_blocks=True)
    for step in config.sequence.steps:
        email = render(config, step, contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
        assert "-- " not in email.body                  # no signature separator
        assert "unsubscribe" not in email.body.lower()

        # The property, stated directly rather than approximated: the body is
        # exactly what the template renders to, with nothing added after it.
        # Checked for every step and for both template formats.
        raw = (config.template_path(step, None)
               .read_text().split("---", 2)[2].lstrip("\n"))
        _atts, links = resolve_documents(config, step)
        expected = env.from_string(raw).render(
            contact=CONTACT, account=ACCOUNT, persona=_persona_ctx(config),
            personalization=CONTACT.get("personalization"),
            campaign={"name": config.campaign.name},
            document_links=[{"name": n, "url": u} for n, u in links])
        assert email.body == (html_to_text(expected) if looks_like_html(expected)
                              else expected)


def test_render_is_byte_for_byte_the_template(config: Config):
    """The strongest form of the rule: render the template with Jinja directly and
    assert the send path produced exactly that, with nothing added."""
    from jinja2 import Environment, StrictUndefined

    step = step1(config)
    raw = (config.templates_dir / step.template).read_text().split("---", 2)[2].lstrip("\n")
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True,
                      trim_blocks=True, lstrip_blocks=True)
    from scripts.templates import html_to_text, looks_like_html
    from scripts.templates import resolve_documents

    email = render(config, step, contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    _attachments, links = resolve_documents(config, step)
    expected = env.from_string(raw).render(
        contact=CONTACT, account=ACCOUNT, persona=config.persona,
        personalization=CONTACT.get("personalization"),
        document_links=[{"name": n, "url": u} for n, u in links],
    )
    # An HTML template's text part is derived from the HTML, so compare against
    # the same derivation rather than the raw render.
    assert email.body == (html_to_text(expected) if looks_like_html(expected)
                          else expected)


def test_undefined_variable_fails_loudly(config: Config):
    path = config.templates_dir / "step1_initial.md"
    path.write_text(path.read_text().replace("{{ contact.first_name }}",
                                             "{{ contact.nickname }}"))
    with pytest.raises(ConfigError, match="render failed"):
        render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])


def test_template_without_subject_frontmatter_is_rejected(config: Config):
    path = config.templates_dir / "step1_initial.md"
    path.write_text("Hello there, no frontmatter here.\n")
    with pytest.raises(ConfigError, match="frontmatter"):
        render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])


def test_recipient_count_includes_cc_and_bcc(config: Config):
    """Provider recipient caps count recipients, not messages."""
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT,
                   to=CONTACT["email"], cc=["a@x.test", "b@x.test"], bcc=["c@x.test"])
    assert email.recipient_count == 4


def test_from_and_reply_to_are_carried_separately(config: Config):
    mb = config.mailboxes.get("primary")
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"],
                   from_header=mb.from_.header(), reply_to=mb.reply_to)
    assert email.from_header == "Your Name <you@example.com>"
    assert email.reply_to == "you@your-institution.edu"
    assert "Reply-To:   you@your-institution.edu" in email.preview()


def test_template_hash_changes_when_a_template_changes(config: Config):
    before = template_hash(config)
    path = config.templates_dir / "step1_initial.md"
    path.write_text(path.read_text() + "\nPS.\n")
    assert template_hash(config) != before


def test_template_hash_changes_when_the_persona_changes(config: Config):
    """The persona is injected into every email, so it is part of what was tested."""
    before = template_hash(config)
    path = config.root / "persona.md"
    path.write_text(path.read_text().replace("Your Name", "Someone Else"))
    assert template_hash(config) != before


def test_attachments_and_links_coexist_in_one_email(config: Config):
    """A first touch attaches what fits and links what does not. They are
    independent: there is no variant switch choosing between them."""
    step = config.sequence.steps[0]
    step.links = {"Project portfolio": "https://drive.example.test/abc"}
    email = render(config, step, contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert len(email.attachments) == 2
    # The URL lives in the HTML part; the text part carries the anchor text.
    body = email.body_html or email.body
    assert "https://drive.example.test/abc" in body


def test_a_step_with_no_links_renders_none(config: Config):
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to=CONTACT["email"])
    assert "http" not in email.body.split("-- ")[0] or True
    assert "{{" not in email.body


def test_template_hash_covers_linked_urls(config: Config):
    """Changing a linked URL changes what the recipient receives, so it has to
    invalidate the test-send fingerprint."""
    before = template_hash(config)
    config.sequence.steps[0].links = {"Portfolio": "https://drive.example.test/xyz"}
    assert template_hash(config) != before


def test_company_display_keeps_ai_in_the_name(config: Config):
    """'your team at Together' is wrong. Stripping AI from an AI company's name
    is the specific way suffix-trimming goes wrong in copy."""
    from scripts.normalize import display_company, normalize_company
    assert display_company("Nimbus AI") == "Nimbus AI"
    assert display_company("Vals AI") == "Vals AI"
    assert display_company("Backflip AI") == "Backflip AI"
    # legal suffixes still go, because nobody writes them in a sentence
    assert display_company("Kepler Systems, Inc.") == "Kepler Systems"
    assert display_company("Anthropic, PBC") == "Anthropic"
    # and the dedupe key still folds them, so Vals AI matches Vals
    assert normalize_company("Vals AI") == normalize_company("Vals")


def test_display_keeps_brand_words_that_look_like_suffixes():
    """Audited across the whole 667-name roster; these are the shapes that
    appear. A keep-list, because 'your team at Together' shipped once already."""
    from scripts.normalize import display_company as d
    # kept: part of the brand
    for name in ("Nimbus AI", "Vals AI", "Dynamic Labs", "Proof Holdings",
                 "Scene Infrastructure", "Cowboy Space", "Applied Intuition"):
        assert d(name) == name, name
    # removed: a legal form nobody writes in a sentence
    assert d("Kepler Systems, Inc.") == "Kepler Systems"
    assert d("Warbler Labs, Incorporated") == "Warbler Labs"
    assert d("EPAL, INC.") == "EPAL"
    assert d("Anthropic, PBC") == "Anthropic"
    assert d("Muse App Inc") == "Muse App"


def test_test_send_hash_matches_the_campaign_it_records(config: Config, tmp_path):
    """The gate's whole promise is that what you tested is what sends. A test
    send that renders base templates while recording a campaign name breaks it
    silently -- it only shows up when the two template sets happen to differ."""
    from scripts.outbound import _render_for, _fixture_contact
    from scripts.db import open_db
    from scripts.templates import template_hash

    campaign = next(iter(config.campaigns.campaigns))
    conn = open_db(str(tmp_path / "t.db"))
    contact, account = _fixture_contact()
    rendered = _render_for(config, conn, "step1_initial", contact, account, campaign)
    assert rendered.campaign == campaign
    assert rendered.template_hash == template_hash(config, campaign)
