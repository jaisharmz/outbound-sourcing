# Setup runbook

Two tracks. The software track finishes in hours. The domain track is calendar time and
cannot be compressed — start it today, because everything at real volume waits on it.

---

## Track A — software (hours)

```bash
cd ~/.claude/skills/outbound-sourcing
uv venv --python 3.13
uv pip install -e ".[dev]"
cp -r config.example config
$EDITOR config/persona.md config/campaign.yaml config/mailboxes.yaml config/cc.yaml
.venv/bin/python -m scripts.outbound validate-config
.venv/bin/python -m scripts.outbound db migrate
.venv/bin/python -m scripts.outbound demo
```

`demo` runs the whole path on fixtures through the `console` mailbox with zero network
calls. If it prints three emails that read correctly, the skeleton is sound.

---

## Track B — sending domains (3–6 weeks, start now)

### 1. Buy domains (day 0)

Two or three, close to the real one — `.com` if available, `.org`/`.io` otherwise. Avoid
the newer bulk TLDs, which carry reputation baggage you did not earn. Never send volume
from a primary personal or institutional domain.

### 2. Google Workspace (day 0–1)

One tenant per domain, one seat per mailbox to start. Business Starter is enough — this
needs Gmail API access, not Vault.

### 3. DNS (day 1)

Per domain:

| Record | Value |
|---|---|
| SPF | `v=spf1 include:_spf.google.com ~all` — exactly one SPF record per domain |
| DKIM | Generate a 2048-bit key in Workspace Admin → Apps → Gmail → Authenticate email, publish the TXT record, then **click Start Authentication** (skipping this is the usual reason DKIM silently fails) |
| DMARC | Start at `v=DMARC1; p=none; rua=mailto:dmarc@<domain>` and move to `p=quarantine` after two clean weeks |

Verify with `dig TXT <selector>._domainkey.<domain>` and a mail-tester service before the
first real send. `test-email` prints the raw headers so alignment can be checked directly.

### 4. OAuth client (day 1)

Google Cloud Console → new project → enable the Gmail API → OAuth consent screen
(Internal, if the tenant allows) → Desktop app credentials. Scopes:

```
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.readonly     # reply detection
https://www.googleapis.com/auth/gmail.compose      # drafts for interested replies
```

Client ID and secret go in `config/secrets.env`, which is gitignored. Each mailbox's
refresh token lands under its `auth_ref` key.

**Check this before anything else if any mailbox lives on an institutional tenant.** Many
campus and enterprise Workspace tenants block third-party OAuth clients from holding
`gmail.send`, via Admin Console → Security → API Controls. Whether a given tenant does is
usually not documented publicly, so **test it rather than reasoning about it**:

```bash
python -m scripts.outbound auth --mailbox <id>
```

The command names the failure mode rather than echoing a stack trace, because these are
not interchangeable:

| Result | Meaning |
|---|---|
| authorized | The tenant permits it. Proceed. |
| `admin_policy_enforced` | API Controls block this client. An admin must allowlist the client ID. Not fixable in code. |
| `access_denied` | Consent declined, **or** the app is in Testing mode and this account is not on the test-users list, **or** the tenant blocks unverified apps. |
| `org_internal` | The consent screen is Internal; only accounts in that org can authorize. |
| HTTP 403 `domainPolicy` | Auth succeeded but the API call is blocked for this account. |

A consumer Gmail account has no admin, so it will authorize — subject to the unverified-app
warning screen, which the account owner can click through.

If an institutional account is blocked, it still works as `Reply-To`. It just cannot be a
sender, which is the arrangement you want anyway.

### 4b. Publishing status — this bites an unattended daemon

While the OAuth consent screen is in **Testing**, Google treats the app as unverified and
**every refresh token it issues expires after 7 days**. A daemon that is supposed to
survive you not looking at it for three days will instead stop every week and need a
human at a browser. Two ways out, and they suit different stages:

**Now, for the test harness on a consumer Gmail account:** add the account under
Google Auth Platform → Audience → Test users. Authorization then works, with the 7-day
expiry. That is fine for a rendering harness you re-auth by hand.

**Before real sending, for the mailbox pool:** don't use installed-app OAuth at all. The
sending domains are Workspace tenants you will be super admin of, which makes a **service
account with domain-wide delegation** the right mechanism:

- no consent screen, so no verification and no unverified-app warning
- no refresh tokens, so nothing expires on a 7-day clock
- **one credential impersonates every mailbox in the domain**, so a 3-mailbox pool and a
  15-mailbox pool cost the same to authorize

Set it up in Admin Console → Security → API Controls → Domain-wide delegation, granting
the service account's client ID the same three Gmail scopes. This does not work for
consumer `@gmail.com` accounts, which is exactly why the interim harness uses OAuth and
the production pool should not.

The alternative — publishing the app to **In production** — also removes the 7-day expiry,
but the Gmail scopes here are sensitive/restricted, so production status invites Google's
verification process. Domain-wide delegation sidesteps that question entirely for domains
you own.

### 4c. If OAuth stays blocked: app password over SMTP

Google's console can be slow or confusing about test users, and the harness does not need
OAuth at all. An app password over SMTP has no consent screen, no verification review, no
publishing status, and therefore no 7-day expiry:

1. Enable 2-Step Verification on the account (required — app passwords are hidden without it).
2. Create one at `myaccount.google.com/apppasswords`.
3. Put it in `config/secrets.env` under the key the mailbox's `auth_ref` names.
4. `python -m scripts.outbound test-email --mailbox <smtp mailbox id>`

The SMTP provider sends with attachments, CC, Reply-To, and an envelope-only Bcc, and
reads the delivered copy's `Authentication-Results` over IMAP — so it answers the
SPF/DKIM/DMARC and From/Reply-To questions just as well as the Gmail API does.

What it does not give is thread IDs, so reply detection falls back to matching
`In-Reply-To`/`References`. That is why the Gmail API stays the preferred provider for the
real pool, and why domain-wide delegation (4b) is the destination rather than either of
these.

### 5. Landing page (before the links A/B)

Host the first-touch documents on the sending domain, so the link domain aligns with the
From domain. Then set `links_base_url` and flip `step1_variant` to `links`.

### 6. Warmup calendar

Per mailbox, from its `warmup_start_date`:

| Week | Per mailbox/day |
|---|---|
| 1 | 10 → 25 |
| 2 | 30 → 45 |
| 3 | 50 (or the configured `daily_cap`) |

Ramp is automatic from `campaign.yaml: warmup`. What is not automatic: the first week
should include real conversations, not only cold sends. Replies from real humans are the
strongest positive signal a new domain can accumulate.

---

## Order of operations

1. Buy domains and set DNS **today**. The clock starts when the records land.
2. Build and test locally against the `console` mailbox meanwhile.
3. OAuth one existing mailbox for `test-email`, purely as a rendering harness. Nothing
   cold ships from it.
4. When the domains are warm, run the seed sends and check placement by hand in Gmail and
   Outlook before opening the tap.
