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


def test_missing_password_says_what_to_create_without_dialling_out(mailbox, monkeypatch):
    """A missing credential must fail before any socket is opened."""
    def no_network(*a, **k):
        raise AssertionError("opened a connection despite having no password")
    monkeypatch.setattr(smtplib, "SMTP", no_network)
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


def test_from_and_reply_to_headers(config, mailbox):
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", from_header=mailbox.mailbox.from_.header(),
                   reply_to=mailbox.mailbox.reply_to)
    msg = mailbox.build_mime(email)
    assert msg["From"] == "Sender Name <sender@sending-domain.test>"
    assert msg["Reply-To"] == "replies@institution.test"


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


def test_message_id_is_set_by_us_not_the_server(config, mailbox):
    """Reply matching finds our Message-ID in a reply's In-Reply-To. An ID the
    server assigned and we never saw is an ID we can never match against."""
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", from_header=mailbox.mailbox.from_.header())
    msg = mailbox.build_mime(email)
    assert msg["Message-ID"]
    assert msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith(">")
    assert "sending-domain.test" in msg["Message-ID"]


def test_message_ids_are_unique_per_message(config, mailbox):
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", from_header=mailbox.mailbox.from_.header())
    assert mailbox.build_mime(email)["Message-ID"] != mailbox.build_mime(email)["Message-ID"]


def test_send_result_carries_the_message_id(config, mailbox, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def send_message(self, msg, from_addr=None, to_addrs=None):
            sent["msg"], sent["to"] = msg, to_addrs

    monkeypatch.setattr(mailbox, "_smtp", lambda: FakeSMTP())
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", cc=["c@x.test"], bcc=["secret@x.test"],
                   from_header=mailbox.mailbox.from_.header())
    result = mailbox.send(email)
    assert result.ok
    assert result.message_id == sent["msg"]["Message-ID"]
    # Bcc is in the envelope only.
    assert sent["to"] == ["ada@target.test", "c@x.test", "secret@x.test"]
    assert sent["msg"]["Bcc"] is None


def test_imap_search_arguments_are_quoted():
    """An unquoted subject with spaces is 'SEARCH command error: BAD'."""
    from scripts.providers.smtp import _imap_quote
    assert _imap_quote("a b / c?") == '"a b / c?"'
    assert _imap_quote('say "hi"') == '"say \\"hi\\""'


def test_delivered_header_failure_returns_none_not_an_exception(mailbox, monkeypatch):
    """The send already succeeded; losing the header dump is not a failed send."""
    def boom():
        raise RuntimeError("imap exploded")
    monkeypatch.setattr(mailbox, "_imap", boom)
    assert mailbox.delivered_headers("subject", timeout_seconds=0) is None
