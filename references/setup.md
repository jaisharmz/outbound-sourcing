# Setup

One mailbox, 15–25 sends a day, invoked by hand. Everything below is what that needs.

## Install

```bash
cd ~/.claude/skills/outbound-sourcing
uv venv --python 3.13
uv pip install -e ".[dev]"
cp -r config.example config          # then edit config/
.venv/bin/python -m scripts.outbound validate-config
.venv/bin/python -m scripts.outbound db migrate
.venv/bin/python -m scripts.outbound demo
```

## The mailbox

One entry in `mailboxes.yaml`, authenticated with an app password over SMTP:

1. Enable 2-Step Verification on the sending account — app passwords do not exist as an
   option without it.
2. Create one at `myaccount.google.com/apppasswords`.
3. Put it in `config/secrets.env` under the key the mailbox's `auth_ref` names. Spaces are
   fine; they get stripped.

No OAuth, so no consent screen, no verification review, and no 7-day refresh-token expiry.

Cold sends go `From:` the personal address with `Reply-To:` the institutional one, which
keeps the `.edu` visible without routing volume through it.

## Check it before the first real send

```bash
outbound test-email --mailbox <id> --step step1_initial
outbound test-email --mailbox <id> --to <one-time@mail-tester.com>
```

The first prints the resolved CC/BCC, attachment paths and wire sizes, the outgoing
headers, and — because the test recipient is the same account — the delivered copy's
`Authentication-Results` with SPF, DKIM and DMARC parsed out. That confirms DKIM signs and
that a `berkeley.edu` Reply-To against a `gmail.com` From is not flagged.

Authentication cannot be measured by mailing yourself from a *different* provider's
perspective, so `--to` a mail-tester address is the second check.

---

## What was removed, and why

This skill was originally specified for 100–500 sends/day across a pool of dedicated
sending domains. The real requirement is 15–25/day from one mailbox, and most of the
volume architecture was overhead at that number. Removed rather than left dormant, because
a knob nobody turns is a knob somebody later trusts:

| removed | why |
|---|---|
| mailbox pool, round-robin | one mailbox |
| warmup ramp | nothing to warm; the address already has history |
| launchd daemon | sends are a foreground command: `outbound send --limit 20` |
| recipient-local sending windows | one operator's window is enough at this volume |
| dedicated sending domains, SPF/DKIM/DMARC runbook | sending from an existing Gmail account whose DKIM is already Google's |
| links-vs-attachments A/B | needs a landing page on a sending domain that no longer exists. Attachments and links now coexist in one email instead of competing |
| bounce circuit breaker | it protected a sending domain from a runaway campaign. At 20/day a bad list is caught by eye first |
| paid verification API tier | MX + SMTP is enough when the review gate sees every row |
| catch-all volume cap | it rationed catch-all sends to protect a domain. `catch_all` now sends normally |
| reply classification, draft generation | the operator reads their own inbox at this volume |

**What deliberately stayed, because it is independent of volume:**

- the evidence contract and the candidate validator — a hallucinated contact is wrong at
  any rate
- the review gate, including the five rendered previews. It is now the only quality
  control before a send, so it matters more, not less
- `max_attachment_bytes` and the campaign gate — gateway rejection does not care how many
  you send
- bounce detection feeding the permanent suppression list — address quality is independent
  of volume
- reply detection, stop-on-reply, and company-level suppression
- crash safety: a message commits `sending` before the provider call and `sent` after
- the `--to` allowlist and its unconditional suppression check
