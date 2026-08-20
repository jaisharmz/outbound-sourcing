"""Console mailbox: renders a message to stdout or a file instead of sending.

This is what milestone 2 runs end to end, and what the test suite uses so no
test ever touches the network.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from ..config import Mailbox
from ..templates import RenderedEmail
from . import IncomingReply, MailboxProvider, SendResult


class ConsoleMailbox(MailboxProvider):
    def __init__(self, mailbox: Mailbox, secrets: dict[str, str] | None = None,
                 stream: TextIO | None = None, outdir: Path | None = None):
        super().__init__(mailbox, secrets)
        self.stream = stream or sys.stdout
        self.outdir = Path(outdir) if outdir else None
        self.sent: list[RenderedEmail] = []

    def send(self, email: RenderedEmail) -> SendResult:
        self.sent.append(email)
        digest = hashlib.sha256(
            f"{email.to}|{email.subject}|{email.body_hash}".encode()
        ).hexdigest()[:16]
        rendered = email.preview()
        print("=" * 72, file=self.stream)
        print(rendered, file=self.stream)
        print("=" * 72, file=self.stream)
        if self.outdir:
            self.outdir.mkdir(parents=True, exist_ok=True)
            (self.outdir / f"{digest}.txt").write_text(rendered)
        return SendResult(
            ok=True,
            message_id=f"console-{digest}",
            thread_id=f"thread-{digest}",
            headers=self._headers(email),
        )

    def _headers(self, email: RenderedEmail) -> str:
        lines = [
            f"From: {email.from_header}",
            f"To: {email.to}",
        ]
        if email.cc:
            lines.append(f"Cc: {', '.join(email.cc)}")
        if email.reply_to:
            lines.append(f"Reply-To: {email.reply_to}")
        lines += [
            f"Subject: {email.subject}",
            f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}",
            "MIME-Version: 1.0",
            "X-Outbound-Variant: " + email.variant,
        ]
        return "\n".join(lines)

    def list_replies(self, thread_ids: list[str], since: datetime | None = None) -> list[IncomingReply]:
        return []

    def create_draft(self, email: RenderedEmail, thread_id: str | None = None) -> SendResult:
        print("--- DRAFT (not sent) ---", file=self.stream)
        print(email.preview(), file=self.stream)
        return SendResult(ok=True, message_id="console-draft", thread_id=thread_id)
