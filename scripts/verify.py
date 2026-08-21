"""Email verification: MX lookup, then an SMTP RCPT probe. No paid tier.

Deterministic, no model. Nothing unverified enters the send queue.

`catch_all` is the expected outcome, not an exception -- most Workspace and M365
domains accept every recipient -- and it sends normally now that no dedicated
sending domain needs rationing. The review gate is what stands between an
inferred address and a stranger, which is why the export shows how each address
was arrived at.
"""

from __future__ import annotations

import random
import smtplib
import socket
import sqlite3
import string

from .db import log_event, utcnow

VALID, CATCH_ALL, INVALID, UNKNOWN = "valid", "catch_all", "invalid", "unknown"
# The domain accepts mail and the mailbox could not be probed, because outbound
# port 25 is blocked here -- as it is on most residential and cloud networks.
# Distinct from `unknown`, which means the probe ran and told us nothing.
MX_ONLY = "mx_only"

_PORT_25: bool | None = None


def port_25_reachable(timeout: int = 8) -> bool:
    """Checked once. Blocked 25 is a property of the network, not of an address."""
    global _PORT_25
    if _PORT_25 is None:
        try:
            server = smtplib.SMTP(timeout=timeout)
            server.connect("aspmx.l.google.com", 25)
            server.quit()
            _PORT_25 = True
        except Exception:
            _PORT_25 = False
    return _PORT_25


def mx_hosts(domain: str, timeout: int = 8) -> list[str]:
    try:
        import dns.resolver
    except ImportError:
        return []
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = resolver.timeout = timeout
        answers = resolver.resolve(domain, "MX")
        return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
    except Exception:
        return []


def _random_local() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=18))


def probe(email: str, *, mail_from: str = "", timeout: int = 10) -> tuple[str, str]:
    """Return (status, detail).

    A domain that accepts a random address accepts everything, so the real
    address tells us nothing beyond "the domain exists" -- that is catch_all,
    and it is reported as such rather than as a pass.
    """
    domain = email.partition("@")[2]
    if not domain:
        return INVALID, "no domain in address"
    hosts = mx_hosts(domain)
    if not hosts:
        return INVALID, f"no MX record for {domain}"

    if not port_25_reachable():
        return MX_ONLY, (f"{domain} has MX ({hosts[0]}) but outbound port 25 is blocked "
                         f"from this network, so the mailbox itself was not probed")

    sender = mail_from or f"probe@{domain}"
    for host in hosts[:2]:
        try:
            server = smtplib.SMTP(timeout=timeout)
            server.connect(host, 25)
            server.helo(socket.getfqdn() or "localhost")
            server.mail(sender)
            code, _ = server.rcpt(email)
            decoy_code, _ = server.rcpt(f"{_random_local()}@{domain}")
            try:
                server.quit()
            except Exception:
                pass
        except (smtplib.SMTPException, OSError) as exc:
            continue

        if code >= 500 and decoy_code >= 500:
            return INVALID, f"{host} rejected the address ({code}) and rejected a decoy"
        if code < 300 and decoy_code < 300:
            return CATCH_ALL, f"{host} accepted a random address too; the domain is accept-all"
        if code < 300:
            return VALID, f"{host} accepted the address and rejected a decoy ({decoy_code})"
        return UNKNOWN, f"{host} answered {code}, decoy {decoy_code}"
    return UNKNOWN, f"no MX host for {domain} completed a probe (blocked or filtered)"


def verify_contacts(conn: sqlite3.Connection, *, limit: int = 50,
                    mail_from: str = "", only_unverified: bool = True) -> dict[str, int]:
    where = "WHERE sendable = 1" + (" AND verification_status = 'unverified'"
                                    if only_unverified else "")
    rows = conn.execute(f"SELECT id, email FROM contacts {where} LIMIT ?",
                        (limit,)).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        status, detail = probe(r["email"], mail_from=mail_from)
        counts[status] = counts.get(status, 0) + 1
        conn.execute("UPDATE contacts SET verification_status = ?, verification_detail = ?,"
                     " verified_at = ?, updated_at = ? WHERE id = ?",
                     (status, detail, utcnow(), utcnow(), r["id"]))
        log_event(conn, "info", "verify", email=r["email"], status=status)
    return counts
