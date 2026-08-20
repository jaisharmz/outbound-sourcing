"""Gmail API mailbox.

Chosen over the community MCP server for the whole path, send and triage alike:
the sender is a daemon running unattended at volume and must not need a model
session alive, and the Mailbox abstraction already requires `list_replies` and
`create_draft`. Running triage through a second mechanism would mean every
mailbox exists twice -- two token stores, two scope sets, two things to re-auth
when a refresh token expires on day 40 -- for capability this layer already has.

Thread IDs are the other reason. They make reply detection reliable in a way
IMAP subject/heuristic matching is not.

Nothing here calls a model. See tests/test_send_path_purity.py.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path
from typing import Any

from ..config import Mailbox
from ..templates import RenderedEmail
from . import IncomingReply, MailboxProvider, SendResult

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

# Headers worth reading off a delivered message. The first three are the only
# way to confirm SPF/DKIM/DMARC alignment for real rather than by inspecting DNS.
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


class GmailAuthError(RuntimeError):
    """Authentication failed in a way the operator has to act on."""


def _require_google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build as build_service
        from googleapiclient.errors import HttpError
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise GmailAuthError(
            "Gmail support needs the optional dependencies. Install them with:\n"
            '  uv pip install -e ".[gmail]"'
        ) from exc
    return Request, Credentials, InstalledAppFlow, build_service, HttpError


def token_path(mailbox_id: str) -> Path:
    root = Path(__file__).resolve().parent.parent.parent / "state" / "tokens"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{mailbox_id}.json"


class GmailMailbox(MailboxProvider):
    def __init__(self, mailbox: Mailbox, secrets: dict[str, str] | None = None):
        super().__init__(mailbox, secrets)
        self._service = None

    # ------------------------------------------------------------------ auth

    def _credentials(self, interactive: bool = False):
        Request, Credentials, InstalledAppFlow, _, _ = _require_google()
        path = token_path(self.mailbox.id)
        creds = None
        if path.exists():
            creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json())
            return creds
        if not interactive:
            raise GmailAuthError(
                f"mailbox {self.mailbox.id!r} has no usable token at {path}.\n"
                f"  Run: python -m scripts.outbound auth --mailbox {self.mailbox.id}"
            )

        client_id = self.secrets.get("GMAIL_CLIENT_ID")
        client_secret = self.secrets.get("GMAIL_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise GmailAuthError(
                "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in config/secrets.env.\n"
                "  Create a Desktop-app OAuth client: Google Cloud Console -> APIs & Services\n"
                "  -> Credentials -> Create credentials -> OAuth client ID -> Desktop app.\n"
                "  Enable the Gmail API on the same project first."
            )
        flow = InstalledAppFlow.from_client_config(
            {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            },
            SCOPES,
        )
        creds = flow.run_local_server(port=0, prompt="consent")
        path.write_text(creds.to_json())
        path.chmod(0o600)
        return creds

    def service(self, interactive: bool = False):
        if self._service is None:
            _, _, _, build_service, _ = _require_google()
            self._service = build_service(
                "gmail", "v1", credentials=self._credentials(interactive), cache_discovery=False
            )
        return self._service

    def authorize(self) -> tuple[bool, str]:
        """Run the interactive OAuth flow. Returns (ok, message).

        The message distinguishes the failure modes that matter, because
        "it didn't work" and "the tenant forbids this" call for different
        responses and only one of them is fixable in code.
        """
        _, _, _, _, HttpError = _require_google()
        try:
            svc = self.service(interactive=True)
            profile = svc.users().getProfile(userId="me").execute()
        except GmailAuthError:
            raise
        except HttpError as exc:  # pragma: no cover - network
            return False, _explain_http_error(exc)
        except Exception as exc:  # pragma: no cover - network
            return False, _explain_oauth_error(exc)

        addr = profile.get("emailAddress", "")
        configured = self.mailbox.from_.address
        if addr.lower() != configured.lower():
            return False, (
                f"authorized {addr}, but mailbox {self.mailbox.id!r} is configured to send "
                f"from {configured}. Re-run and pick the right account, or fix "
                f"mailboxes.yaml."
            )
        return True, f"authorized {addr} ({profile.get('messagesTotal', '?')} messages)"

    def verify_auth(self) -> tuple[bool, str]:
        _, _, _, _, HttpError = _require_google()
        try:
            profile = self.service().users().getProfile(userId="me").execute()
            return True, f"ok: {profile.get('emailAddress')}"
        except GmailAuthError as exc:
            return False, str(exc)
        except HttpError as exc:  # pragma: no cover - network
            return False, _explain_http_error(exc)

    # ------------------------------------------------------------------ send

    def build_mime(self, email: RenderedEmail) -> EmailMessage:
        msg = EmailMessage()
        name, addr = parseaddr(email.from_header or self.mailbox.from_.header())
        msg["From"] = formataddr((name, addr))
        msg["To"] = email.to
        if email.cc:
            msg["Cc"] = ", ".join(email.cc)
        if email.bcc:
            # Gmail delivers to Bcc recipients from this header and strips it
            # from what the recipient sees.
            msg["Bcc"] = ", ".join(email.bcc)
        if email.reply_to:
            msg["Reply-To"] = email.reply_to
        msg["Subject"] = email.subject
        # Stamped so reply/bounce rate can be broken down per arm of the A/B.
        msg["X-Outbound-Variant"] = email.variant
        msg["X-Outbound-Step"] = email.step_id
        msg.set_content(email.body)

        for att in email.attachments:
            ctype, _ = mimetypes.guess_type(att.name)
            maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
            msg.add_attachment(
                att.path.read_bytes(), maintype=maintype, subtype=subtype, filename=att.name
            )
        return msg

    def send(self, email: RenderedEmail, thread_id: str | None = None) -> SendResult:
        _, _, _, _, HttpError = _require_google()
        msg = self.build_mime(email)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        try:
            sent = self.service().users().messages().send(userId="me", body=body).execute()
        except GmailAuthError as exc:
            return SendResult(ok=False, error=str(exc), retryable=False)
        except HttpError as exc:
            retryable = exc.resp.status in (429, 500, 502, 503, 504)
            return SendResult(ok=False, error=_explain_http_error(exc), retryable=retryable)
        return SendResult(
            ok=True,
            message_id=sent.get("id"),
            thread_id=sent.get("threadId"),
            headers=self.outgoing_headers(msg),
        )

    def outgoing_headers(self, msg: EmailMessage) -> str:
        return "\n".join(f"{k}: {v}" for k, v in msg.items())

    # ------------------------------------------------------- delivered copy

    def delivered_headers(self, subject: str, timeout_seconds: int = 30) -> str | None:
        """Fetch the *received* copy's headers, if it landed in this mailbox.

        SPF/DKIM/DMARC results only exist on the delivered message -- the sent
        copy has no Authentication-Results. When the test recipient is the same
        account that sent, this is how alignment gets confirmed for real.
        """
        _, _, _, _, HttpError = _require_google()
        query = f'subject:"{subject}" newer_than:1d'
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                res = (
                    self.service()
                    .users()
                    .messages()
                    .list(userId="me", q=query, labelIds=["INBOX"], maxResults=1)
                    .execute()
                )
            except HttpError:  # pragma: no cover - network
                return None
            messages = res.get("messages", [])
            if messages:
                full = (
                    self.service()
                    .users()
                    .messages()
                    .get(
                        userId="me",
                        id=messages[0]["id"],
                        format="metadata",
                        metadataHeaders=AUTH_HEADERS,
                    )
                    .execute()
                )
                headers = full.get("payload", {}).get("headers", [])
                order = {h: i for i, h in enumerate(AUTH_HEADERS)}
                headers.sort(key=lambda h: order.get(h["name"], 99))
                return "\n".join(f"{h['name']}: {h['value']}" for h in headers)
            time.sleep(3)
        return None

    # ----------------------------------------------------------- replies

    def list_replies(
        self, thread_ids: list[str], since: datetime | None = None
    ) -> list[IncomingReply]:
        _, _, _, _, HttpError = _require_google()
        out: list[IncomingReply] = []
        sent_ids = set()
        for tid in thread_ids:
            try:
                thread = self.service().users().threads().get(userId="me", id=tid).execute()
            except HttpError:  # pragma: no cover - network
                continue
            for msg in thread.get("messages", []):
                if "SENT" in msg.get("labelIds", []):
                    sent_ids.add(msg["id"])
                    continue
                headers = {
                    h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
                }
                received = datetime.fromtimestamp(
                    int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc
                )
                if since and received <= since:
                    continue
                out.append(
                    IncomingReply(
                        provider_id=msg["id"],
                        thread_id=tid,
                        from_addr=parseaddr(headers.get("From", ""))[1],
                        subject=headers.get("Subject", ""),
                        body=msg.get("snippet", ""),
                        received_at=received,
                        headers=headers,
                    )
                )
        return out

    def create_draft(self, email: RenderedEmail, thread_id: str | None = None) -> SendResult:
        """Interested replies get a draft. Never an auto-send to a human."""
        _, _, _, _, HttpError = _require_google()
        msg = self.build_mime(email)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        try:
            draft = (
                self.service()
                .users()
                .drafts()
                .create(userId="me", body={"message": message})
                .execute()
            )
        except HttpError as exc:
            return SendResult(ok=False, error=_explain_http_error(exc), retryable=False)
        return SendResult(
            ok=True, message_id=draft.get("id"), thread_id=draft.get("message", {}).get("threadId")
        )


def _explain_http_error(exc) -> str:
    """Turn a Gmail API error into something that says what to do next."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        detail = json.loads(exc.content.decode()).get("error", {})
        reason = (detail.get("errors") or [{}])[0].get("reason", "")
        message = detail.get("message", "")
    except Exception:
        reason, message = "", str(exc)

    if status == 403 and reason in ("domainPolicy", "forbidden"):
        return (
            f"HTTP 403 {reason}: the Workspace tenant blocks this app from using the Gmail "
            f"API on this account. This is an admin policy, not a code problem -- the "
            f"account can still serve as Reply-To, but it cannot be a sender. ({message})"
        )
    if status == 403 and reason == "rateLimitExceeded":
        return f"HTTP 403 rate limited: {message}"
    if status == 429:
        return f"HTTP 429 quota exceeded: {message}"
    if status == 400 and "Recipient address required" in message:
        return "HTTP 400: no recipient on the message -- check the To: header"
    return f"HTTP {status} {reason}: {message}".strip()


def _explain_oauth_error(exc) -> str:
    text = str(exc)
    if "admin_policy_enforced" in text:
        return (
            "OAuth refused: admin_policy_enforced. The Workspace tenant's API controls "
            "block this OAuth client from holding these scopes. An admin would have to "
            "allowlist the client ID. Until then this account cannot be a sender -- keep "
            "it as Reply-To and send from a domain you control."
        )
    if "access_denied" in text:
        return (
            "OAuth refused: access_denied. Either consent was declined, or the app is in "
            "Testing mode and this account is not on the test-users list, or the tenant "
            "blocks unverified apps."
        )
    if "org_internal" in text:
        return (
            "OAuth refused: org_internal. The consent screen is set to Internal, so only "
            "accounts in the same Workspace org can authorize it."
        )
    if "disallowed_useragent" in text:
        return "OAuth refused: disallowed_useragent. Complete the flow in a real browser."
    return f"OAuth failed: {text}"
