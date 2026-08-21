"""MIME construction for the Gmail provider. Pure function, no network."""

from __future__ import annotations

import pytest

from scripts.config import Mailbox
from scripts.providers.gmail import GmailMailbox
from scripts.templates import render

CONTACT = {
    "first_name": "Ada", "last_name": "Lovelace", "name": "Ada Lovelace",
    "title": "Research Scientist", "email": "ada@target.test", "personalization": None,
}
ACCOUNT = {"name": "Target Labs", "domain": "target.test"}


@pytest.fixture
def gmail() -> GmailMailbox:
    mb = Mailbox.model_validate({
        "id": "outreach-01",
        "provider": "gmail",
        "from": {"name": "Sender Name", "address": "sender@sending-domain.test"},
        "reply_to": "replies@institution.test",
    })
    return GmailMailbox(mb, {})


def build(config, gmail, **kw):
    email = render(config, config.sequence.steps[0], contact=CONTACT, account=ACCOUNT,
                   to="ada@target.test", from_header=gmail.mailbox.from_.header(),
                   reply_to=gmail.mailbox.reply_to, **kw)
    return email, gmail.build_mime(email)


def test_from_and_reply_to_are_distinct_headers(config, gmail):
    _, msg = build(config, gmail)
    assert msg["From"] == "Sender Name <sender@sending-domain.test>"
    assert msg["Reply-To"] == "replies@institution.test"


def test_cc_and_bcc_headers(config, gmail):
    _, msg = build(config, gmail, cc=["a@x.test", "b@x.test"], bcc=["c@x.test"])
    assert msg["Cc"] == "a@x.test, b@x.test"
    assert msg["Bcc"] == "c@x.test"


def test_no_cc_header_when_there_is_no_cc(config, gmail):
    _, msg = build(config, gmail)
    assert msg["Cc"] is None
    assert msg["Bcc"] is None


def test_attachments_are_attached_with_the_right_type(config, gmail):
    _, msg = build(config, gmail)
    parts = [p for p in msg.iter_attachments()]
    assert len(parts) == 2
    assert all(p.get_content_type() == "application/pdf" for p in parts)
    assert {p.get_filename() for p in parts} == {
        "example_document_a.pdf", "example_document_b.pdf"
    }


def test_body_survives_encoding(config, gmail):
    email, msg = build(config, gmail)
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Hello Ada!" in body
    assert "unsubscribe" not in body.lower()   # nothing is appended


def test_no_tracking_pixel(config, gmail):
    _, msg = build(config, gmail)
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "<img" not in body.lower()


def test_oauth_errors_are_explained_not_echoed():
    from scripts.providers.gmail import _explain_oauth_error

    msg = _explain_oauth_error(Exception("error: admin_policy_enforced"))
    assert "admin policy" in msg.lower() or "API controls" in msg
    assert "Reply-To" in msg

    assert "Testing mode" in _explain_oauth_error(Exception("access_denied"))
    assert "Internal" in _explain_oauth_error(Exception("org_internal"))
