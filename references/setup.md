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

**Check this before anything else if any mailbox lives on an institutional tenant:** many
campus and enterprise Workspace tenants block third-party OAuth clients from holding
`gmail.send`. It is an admin policy, not a code problem, and it is better discovered on
day one than during milestone 3. If it is blocked, that account can still be the
`Reply-To` — it just cannot be a sender.

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
