"""SMTP + IMAP mailbox.

The generic provider, and the fallback when OAuth is unavailable. For a consumer
Gmail account an app password needs no consent screen, no verification review,
and no publishing status -- which also means no 7-day refresh-token expiry.

Gmail API remains the better provider for the real pool because its thread IDs
make reply detection reliable, but everything milestone 3a needs -- a real email
with real attachments and the delivered copy's Authentication-Results -- this can
do today.

Nothing here calls a model. See tests/test_send_path_purity.py.
"""

from __future__ import annotations

import email
import imaplib
import time
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime

from ..config import Mailbox
from ..templates import RenderedEmail
from . import IncomingReply, MailboxProvider, SendResult

AUTH_HEADERS = [
    "Authentication-Results",
    "ARC-Authentication-Results",
    "Received-SPF",
    "DKIM-Signature",
    "From",
    "Reply-To",
    "To",
    "Cc",
    "Subject",
    "Message-ID",
    "Date",
    "Return-Path",
]

# Permanent SMTP failures. A 5xx is a decision, not a hiccup, and retrying one
# is how a campaign turns a rejection into a reputation problem.
PERMANENT = (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
             smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError)


class SMTPMailbox(MailboxProvider):
    def __init__(self, mailbox: Mailbox, secrets: dict[str, str] | None = None):
        super().__init__(mailbox, secrets)

    # ------------------------------------------------------------------ auth

    def _password(self) -> str:
        key = self.mailbox.auth_ref
        if not key:
            raise RuntimeError(
                f"mailbox {self.mailbox.id!r} has no auth_ref naming the secrets.env key "
                f"that holds its password"
            )
        pw = self.secrets.get(key)
        if not pw:
            raise RuntimeError(
                f"mailbox {self.mailbox.id!r}: {key} is not set in config/secrets.env.\n"
                f"  For Gmail this is an App Password, not the account password. Enable\n"
                f"  2-Step Verification, then create one at myaccount.google.com/apppasswords."
            )
        return pw.replace(" ", "")   # Google displays app passwords in groups of four

    def verify_auth(self) -> tuple[bool, str]:
        try:
            with self._smtp() as server:
                server.noop()
            return True, f"ok: {self.mailbox.login} via {self.mailbox.smtp_host}"
        except Exception as exc:
            return False, _explain(exc)

    def authorize(self) -> tuple[bool, str]:
        """No interactive flow. Either the app password works or it does not."""
        return self.verify_auth()

    def _smtp(self) -> smtplib.SMTP:
        # Read the credential before opening a socket. Otherwise a missing app
        # password still dials the provider and waits out the timeout, which is
        # slow when it happens and a network call in a test suite that must not
        # make one.
        password = self._password()
        server = smtplib.SMTP(self.mailbox.smtp_host, self.mailbox.smtp_port, timeout=30)
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(self.mailbox.login, password)
        return server

    def _imap(self, timeout: int = 60) -> imaplib.IMAP4_SSL:
        """Connect to IMAP with a socket timeout.

        Without one, imaplib blocks on read forever. Gmail stopped answering
        partway through a 200-draft run and the process sat on an established
        socket at 0% CPU for half an hour -- indistinguishable from working, and
        it would never have returned. A retryable error after 60s is recoverable
        because create_draft is idempotent on the message key; a hang is not.
        """
        password = self._password()
        conn = imaplib.IMAP4_SSL(self.mailbox.imap_host, self.mailbox.imap_port,
                                 timeout=timeout)
        conn.login(self.mailbox.login, password)
        return conn

    # ------------------------------------------------------------------ send

    def build_mime(self, email_obj: RenderedEmail) -> EmailMessage:
        msg = EmailMessage()
        name, addr = parseaddr(email_obj.from_header or self.mailbox.from_.header())
        msg["From"] = formataddr((name, addr))
        msg["To"] = email_obj.to
        if email_obj.cc:
            msg["Cc"] = ", ".join(email_obj.cc)
        if email_obj.reply_to:
            msg["Reply-To"] = email_obj.reply_to
        msg["Subject"] = email_obj.subject
        # Generated here, not left to the server. Reply matching over IMAP works
        # by finding our Message-ID in a reply's In-Reply-To/References, so an
        # ID we never saw is an ID we can never match against.
        msg["Message-ID"] = make_msgid(domain=addr.partition("@")[2] or None)
        msg["X-Outbound-Step"] = email_obj.step_id
        # set_content then add_alternative gives multipart/alternative with the
        # text part first, which is the order every client expects: the last
        # part wins in renderers that understand HTML, and the first is what a
        # text-only client shows.
        msg.set_content(email_obj.body)
        if email_obj.is_html:
            msg.add_alternative(email_obj.body_html, subtype="html")

        import mimetypes

        for att in email_obj.attachments:
            ctype, _ = mimetypes.guess_type(att.name)
            maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
            msg.add_attachment(
                att.path.read_bytes(), maintype=maintype, subtype=subtype, filename=att.name
            )
        return msg

    def create_draft(self, email_obj: RenderedEmail) -> SendResult:
        """APPEND the message to Gmail's Drafts folder over IMAP.

        Gmail's own draft API needs OAuth, and this project's OAuth client is
        still behind Google's unverified-app wall. IMAP APPEND with the \\Draft
        flag produces exactly the same thing -- a real draft, openable and
        sendable from the Gmail client -- using the app password that already
        works for sending.

        Bcc is written as a header here, unlike on the send path. A draft has no
        envelope: whatever Gmail is going to send is what the headers say, so
        dropping Bcc would silently lose recipients the operator asked for.
        """
        msg = self.build_mime(email_obj)
        if email_obj.bcc:
            msg["Bcc"] = ", ".join(email_obj.bcc)
        try:
            conn = self._imap()
            try:
                folder = self.mailbox.drafts_folder or "[Gmail]/Drafts"
                status, detail = conn.append(
                    f'"{folder}"', "\\Draft", imaplib.Time2Internaldate(time.time()),
                    msg.as_bytes())
                if status != "OK":
                    return SendResult(ok=False, retryable=True,
                                      error=f"IMAP APPEND to {folder!r} returned "
                                            f"{status}: {detail}")
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            return SendResult(ok=False, error=_explain(exc), retryable=True)
        return SendResult(ok=True, message_id=msg["Message-ID"], thread_id=None,
                          headers="\n".join(f"{k}: {v}" for k, v in msg.items()))

    def send(self, email_obj: RenderedEmail) -> SendResult:
        msg = self.build_mime(email_obj)
        # Bcc goes in the envelope only, never in a header SMTP would transmit.
        recipients = [email_obj.to] + list(email_obj.cc) + list(email_obj.bcc)
        try:
            with self._smtp() as server:
                server.send_message(msg, from_addr=parseaddr(msg["From"])[1],
                                    to_addrs=recipients)
        except PERMANENT as exc:
            return SendResult(ok=False, error=_explain(exc), retryable=False)
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as exc:
            return SendResult(ok=False, error=_explain(exc), retryable=True)
        except Exception as exc:
            # Ambiguous: never silently retried. The caller decides.
            return SendResult(ok=False, error=_explain(exc), retryable=False)
        return SendResult(
            ok=True,
            message_id=msg["Message-ID"],
            thread_id=None,
            headers="\n".join(f"{k}: {v}" for k, v in msg.items()),
        )

    # ------------------------------------------------------- delivered copy

    def delivered_headers(self, subject: str, timeout_seconds: int = 30,
                          message_id: str | None = None) -> str | None:
        """Read the received copy's headers over IMAP.

        SPF/DKIM/DMARC verdicts exist only on the delivered message. When the
        test recipient is this same mailbox, this is how alignment gets checked
        for real rather than inferred from DNS.
        """
        import time

        # Message-ID is exact; subject is the fallback for servers that rewrite
        # it. Both are quoted -- an unquoted subject with spaces or punctuation
        # is what "SEARCH command error: BAD Could not parse command" means.
        criteria = []
        if message_id:
            criteria.append(("HEADER", "Message-ID", _imap_quote(message_id)))
        criteria.append(("HEADER", "Subject", _imap_quote(subject)))

        deadline = time.time() + timeout_seconds
        while True:
            try:
                conn = self._imap()
                try:
                    conn.select("INBOX")
                    for crit in criteria:
                        typ, data = conn.search(None, *crit)
                        ids = data[0].split() if data and data[0] else []
                        if not ids:
                            continue
                        typ, raw = conn.fetch(ids[-1], "(BODY.PEEK[HEADER])")
                        blob = raw[0][1].decode("utf-8", "replace")
                        parsed = email.message_from_string(blob)
                        order = {h.lower(): i for i, h in enumerate(AUTH_HEADERS)}
                        items = [(k, v) for k, v in parsed.items() if k.lower() in order]
                        items.sort(key=lambda kv: order[kv[0].lower()])
                        return "\n".join(f"{k}: {v}" for k, v in items)
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            except Exception:
                # Reading the delivered copy is a convenience. The send already
                # succeeded, and losing the header dump must not look like a
                # failed send.
                return None
            if time.time() >= deadline:
                return None
            time.sleep(3)

    # ----------------------------------------------------------- replies

    def list_replies(
        self, thread_ids: list[str], since: datetime | None = None
    ) -> list[IncomingReply]:
        """IMAP has no thread IDs, so replies are matched on In-Reply-To/References.

        This is why the Gmail API is the better provider for the real pool.
        """
        wanted = {t for t in thread_ids if t}
        out: list[IncomingReply] = []
        try:
            conn = self._imap()
        except Exception:
            return out
        try:
            conn.select("INBOX")
            typ, data = conn.search(None, "ALL")
            for num in (data[0].split() if data and data[0] else [])[-200:]:
                typ, raw = conn.fetch(num, "(BODY.PEEK[HEADER])")
                if not raw or not raw[0]:
                    continue
                parsed = email.message_from_string(raw[0][1].decode("utf-8", "replace"))
                refs = f"{parsed.get('In-Reply-To','')} {parsed.get('References','')}"
                match = next((t for t in wanted if t in refs), None)
                if not match:
                    continue
                try:
                    received = parsedate_to_datetime(parsed.get("Date", ""))
                except Exception:
                    received = datetime.now(timezone.utc)
                if since and received <= since:
                    continue
                out.append(
                    IncomingReply(
                        provider_id=parsed.get("Message-ID", ""),
                        thread_id=match,
                        from_addr=parseaddr(parsed.get("From", ""))[1],
                        subject=parsed.get("Subject", ""),
                        body="",
                        received_at=received,
                        headers=dict(parsed.items()),
                    )
                )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out



def _imap_quote(value: str) -> str:
    """Quote a string for an IMAP SEARCH argument."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _explain(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if "Application-specific password required" in text or "534" in text:
            return (
                "SMTP auth rejected: Google requires an App Password, not the account "
                "password. Enable 2-Step Verification, then create one at "
                "myaccount.google.com/apppasswords and put it in secrets.env."
            )
        if "535" in text:
            return (
                "SMTP auth rejected (535). The App Password is wrong, or 2-Step "
                "Verification is not enabled on the account, or the password was pasted "
                "with the account's own password by mistake."
            )
        return f"SMTP auth rejected: {text}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"every recipient was refused: {exc.recipients}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"sender refused: {text}"
    return f"{type(exc).__name__}: {text}"
