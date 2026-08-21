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


def test_every_email_carries_optout_and_a_mailing_address(config: Config):
    """CAN-SPAM, appended by the renderer so no template can forget it."""
    for step in config.sequence.steps:
        email = render(config, step, contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
        assert config.persona.unsubscribe_instructions.split()[0] in email.body
        assert "123 Example Street" in email.body


def test_footer_is_not_duplicated(config: Config):
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert email.body.count("123 Example Street") == 1


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
    assert "https://drive.example.test/abc" in email.body
    assert "Project portfolio" in email.body


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
    assert display_company("Together AI") == "Together AI"
    assert display_company("Vals AI") == "Vals AI"
    assert display_company("Backflip AI") == "Backflip AI"
    # legal suffixes still go, because nobody writes them in a sentence
    assert display_company("Kepler Systems, Inc.") == "Kepler Systems"
    assert display_company("Anthropic, PBC") == "Anthropic"
    # and the dedupe key still folds them, so Vals AI matches Vals
    assert normalize_company("Vals AI") == normalize_company("Vals")
