"""Mailbox providers.

The send path contains zero model calls. Nothing in this package, or anything it
imports, may reach a model -- that is verifiable by reading the import graph
rather than by trusting a comment.

Gmail API first (thread IDs make reply detection far more reliable than IMAP
heuristics), generic SMTP+IMAP second, and `console` for testing.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Mailbox
from ..templates import RenderedEmail


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    thread_id: str | None = None
    headers: str = ""
    error: str | None = None
    # Distinguishes "the provider said no, permanently" from "try again later".
    # An ambiguous failure is never silently retried.
    retryable: bool = False


@dataclass
class IncomingReply:
    provider_id: str
    thread_id: str
    from_addr: str
    subject: str
    body: str
    received_at: datetime
    headers: dict[str, str] = field(default_factory=dict)


class MailboxProvider(abc.ABC):
    """One configured sending identity."""

    def __init__(self, mailbox: Mailbox, secrets: dict[str, str] | None = None):
        self.mailbox = mailbox
        self.secrets = secrets or {}

    @property
    def id(self) -> str:
        return self.mailbox.id

    @abc.abstractmethod
    def send(self, email: RenderedEmail) -> SendResult: ...

    def create_draft(self, email: RenderedEmail) -> SendResult:
        """Write the message as a draft instead of sending it.

        Not abstract: a provider that cannot make drafts should say so at the
        point of use rather than force every provider to carry a stub.
        """
        return SendResult(ok=False, retryable=False,
                          error=f"provider {type(self).__name__} cannot create drafts")

    def delete_draft(self, message_id: str) -> tuple[bool, str]:
        """Remove a draft this provider wrote, named by its Message-ID.

        The counterpart of `create_draft`, and required by the send path rather
        than a convenience: once a queued draft has actually been sent, the copy
        sitting in the operator's Drafts folder is a duplicate waiting for
        someone to click send on it.
        """
        return False, f"provider {type(self).__name__} cannot delete drafts"

    @abc.abstractmethod
    def list_replies(self, thread_ids: list[str], since: datetime | None = None) -> list[IncomingReply]: ...

    def verify_auth(self) -> tuple[bool, str]:
        """Cheap credential check so a daemon fails at startup, not at 2am."""
        return True, "no auth required"


_REGISTRY: dict[str, type[MailboxProvider]] = {}


def register(name: str, cls: type[MailboxProvider]) -> None:
    _REGISTRY[name] = cls


def build(mailbox: Mailbox, secrets: dict[str, str] | None = None) -> MailboxProvider:
    if mailbox.provider not in _REGISTRY and mailbox.provider in _LAZY:
        _LAZY[mailbox.provider]()
    if mailbox.provider not in _REGISTRY:
        raise KeyError(
            f"mailbox {mailbox.id!r} uses provider {mailbox.provider!r}, which is not "
            f"registered. Available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[mailbox.provider](mailbox, secrets)


from .console import ConsoleMailbox  # noqa: E402  (registers itself)

register("console", ConsoleMailbox)


def _load_gmail() -> None:
    """Imported lazily so console-only use never needs the Google libraries."""
    if "gmail" in _REGISTRY:
        return
    from .gmail import GmailMailbox

    register("gmail", GmailMailbox)


def _load_smtp() -> None:
    if "smtp" in _REGISTRY:
        return
    from .smtp import SMTPMailbox

    register("smtp", SMTPMailbox)


_LAZY = {"gmail": _load_gmail, "smtp": _load_smtp}


__all__ = [
    "MailboxProvider", "SendResult", "IncomingReply", "ConsoleMailbox",
    "build", "register",
]
