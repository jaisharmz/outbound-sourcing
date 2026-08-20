"""SMTP provider: MIME shape and failure classification. No network."""

from __future__ import annotations

import smtplib

import pytest

from scripts.config import Mailbox
from scripts.providers.smtp import SMTPMailbox, _explain
from scripts.templates import render

CONTACT = {
    "first_name": "Ada", "last_name": "Lovelace", "name": "Ada Lovelace",
    "title": "Research Scientist", "email": "ada@target.test", "personalization": None,
}
ACCOUNT = {"name": "Target Labs", "domain": "target.test"}


@pytest.fixture
def mailbox() -> SMTPMailbox:
    mb = Mailbox.model_validate({
        "id": "smtp-01", "provider": "smtp",
        "from": {"name": "Sender Name", "address": "sender@sending-domain.test"},
        "reply_to": "replies@institution.test",
        "auth_ref": "SMTP_PASSWORD",
    })
    return SMTPMailbox(mb, {"SMTP_PASSWORD": "abcd efgh ijkl mnop"})


def test_username_defaults_to_the_from_address(mailbox):
    assert mailbox.mailbox.login == "sender@sending-domain.test"


def test_app_password_spaces_are_stripped(mailbox):
    """Google displays app passwords in groups of four; pasting them must work."""
    assert mailbox._password() == "abcdefghijklmnop"


def test_missing_password_says_what_to_create(mailbox):
    mailbox.secrets = {}
    ok, msg = mailbox.verify_auth()
    assert not ok
    assert "App Password" in msg


def test_bcc_is_not_written_into_a_header(config, mailbox):
    """Bcc belongs in the SMTP envelope; a Bcc header would leak the list."""
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", cc=["c@x.test"], bcc=["secret@x.test"],
                   from_header=mailbox.mailbox.from_.header(),
                   reply_to=mailbox.mailbox.reply_to)
    msg = mailbox.build_mime(email)
    assert msg["Bcc"] is None
    assert msg["Cc"] == "c@x.test"
    assert "secret@x.test" not in msg.as_string()


def test_from_reply_to_and_variant_headers(config, mailbox):
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", from_header=mailbox.mailbox.from_.header(),
                   reply_to=mailbox.mailbox.reply_to)
    msg = mailbox.build_mime(email)
    assert msg["From"] == "Sender Name <sender@sending-domain.test>"
    assert msg["Reply-To"] == "replies@institution.test"
    assert msg["X-Outbound-Variant"] == "attachments"


def test_attachments_are_attached(config, mailbox):
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", from_header=mailbox.mailbox.from_.header())
    msg = mailbox.build_mime(email)
    assert len(list(msg.iter_attachments())) == 2


def test_permanent_failures_are_not_marked_retryable():
    """A 5xx is a decision. Silently retrying one is how a rejection becomes a
    reputation problem."""
    assert "App Password" in _explain(
        smtplib.SMTPAuthenticationError(534, b"Application-specific password required")
    )
    assert "535" in _explain(smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    assert "refused" in _explain(smtplib.SMTPRecipientsRefused({"a@b.test": (550, b"no")}))
