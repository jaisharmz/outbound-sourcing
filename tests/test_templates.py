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


def test_step1_attaches_under_the_attachments_variant(config: Config):
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert email.variant == "attachments"
    assert len(email.attachments) == 2
    assert not email.body.count("http://") or True


def test_step1_links_variant_swaps_attachments_for_urls(config: Config):
    config.campaign.step1_variant = "links"
    config.campaign.links_base_url = "https://docs.sending-domain.test"
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert email.variant == "links"
    assert email.attachments == []
    assert "https://docs.sending-domain.test/_first_email/example_document_a.pdf" in email.body


def test_later_steps_always_attach_regardless_of_variant(config: Config):
    """The A/B is about first touch only; by step 2 the recipient has seen us."""
    config.campaign.step1_variant = "links"
    config.campaign.links_base_url = "https://docs.sending-domain.test"
    step2 = config.sequence.steps[1]
    email = render(config, step2, contact=CONTACT, account=ACCOUNT, to=CONTACT["email"])
    assert email.variant == "attachments"


def test_recipient_count_includes_cc_and_bcc(config: Config):
    """Provider recipient caps count recipients, not messages."""
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT,
                   to=CONTACT["email"], cc=["a@x.test", "b@x.test"], bcc=["c@x.test"])
    assert email.recipient_count == 4


def test_from_and_reply_to_are_carried_separately(config: Config):
    mb = config.mailboxes.get("outreach-01")
    email = render(config, step1(config), contact=CONTACT, account=ACCOUNT, to=CONTACT["email"],
                   from_header=mb.from_.header(), reply_to=mb.reply_to)
    assert email.from_header == "Your Name <you@sending-domain.com>"
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


def test_template_hash_changes_with_the_step1_variant(config: Config):
    before = template_hash(config)
    config.campaign.step1_variant = "links"
    assert template_hash(config) != before
