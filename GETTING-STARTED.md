# Getting started

Install, configure, and run. Everything here works today; commands marked
**(milestone N)** are named so you know where they land, and will error until built.

---

## 1. Install

```bash
cd ~/.claude/skills/outbound-sourcing
uv venv --python 3.13
uv pip install -e ".[dev]"
```

Everything below assumes `.venv/bin/python`. Activate the venv (`source .venv/bin/activate`)
if you would rather type `python`.

Verify:

```bash
.venv/bin/python -m pytest        # 88 tests, no network
```

---

## 2. Configure

`config/` is the entire user-facing surface. It is gitignored; `config.example/` is the
shipped template. If `config/` does not exist yet:

```bash
cp -r config.example config
```

| File | What to put in it |
|---|---|
| `persona.md` | Name, role, org, project bullets, links, **physical mailing address**, opt-out wording. Injected into every template. |
| `icp.yaml` | Titles that count, exclusions, regions, `max_contacts_per_company`. |
| `campaign.yaml` | Caps, sending window, jitter, warmup, circuit breaker, verification, `test_recipient`, `attachments_root`, `step1_variant`. |
| `mailboxes.yaml` | The pool. Per-mailbox `from`, `reply_to`, `daily_cap`, `warmup_start_date`, `enabled`. |
| `sequence.yaml` | Steps, delays in business days, attachment sets. |
| `cc.yaml` | Who gets copied, and at which precedence level. |
| `dorks.yaml` | Search seeds for discovery. |
| `blackout_dates.yaml` | Days to never send. |
| `secrets.env` | OAuth and API keys. Never committed. |
| `templates/` | The email copy. YAML frontmatter `subject:`, body below. |

After any edit:

```bash
.venv/bin/python -m scripts.outbound validate-config
```

This is not a formality. It catches typos with a suggestion (`unknown key
'dailyglobalcap' — did you mean 'daily_global_cap'?`), missing templates, missing
attachment files, and undefined attachment sets. A config that loads is a config that
will not surprise you at send time.

### One thing to fill in before any real send

`persona.md` currently has `[STREET ADDRESS NEEDED]`. CAN-SPAM requires a real physical
mailing address in every commercial solicitation, and the footer is appended
automatically to every template — so the placeholder would go out on all of them.

---

## 3. Initialize the database

```bash
.venv/bin/python -m scripts.outbound db migrate
.venv/bin/python -m scripts.outbound db stats
```

Migrations are forward-only and idempotent; re-running is safe. The DB lives at
`state/prospects.db`.

---

## 4. See it work end to end

```bash
.venv/bin/python -m scripts.outbound demo
```

Ingests three fixture companies, shows what landed in the database with evidence counts,
then renders and "sends" every contact through the `console` mailbox. Zero network calls.

Read the output. It shows you the real thing: resolved CC, From/Reply-To split,
attachment names and sizes, the rendered body, and the compliance footer.

---

## 5. Authorize a mailbox and send yourself a real email

```bash
.venv/bin/python -m scripts.outbound auth --mailbox gmail-personal
.venv/bin/python -m scripts.outbound test-email --mailbox gmail-personal --step step1_initial
```

`auth` needs `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` in `config/secrets.env` — see
`references/setup.md` for creating the OAuth client. It reports the precise failure mode
if a Workspace tenant refuses, which matters because a declined consent and an admin
policy call for completely different responses.

`test-email` renders a fully real email — real template, persona, attachments, CC,
footer, headers — sends it to `test_recipient`, and prints:

- the resolved CC/BCC and the recipient count the daily cap will charge
- attachment paths, sizes, and the total **wire** size after base64
- the outgoing headers
- the **delivered** headers, fetched by exact Message-ID match

To get SPF/DKIM/DMARC verdicts you must send to **another provider** — a message from an
account to itself never crosses an authentication boundary, so no verdict headers are
added at all:

```bash
.venv/bin/python -m scripts.outbound test-email --mailbox <id> --to you@outlook.com
.venv/bin/python -m scripts.outbound test-email --mailbox <id> --to <one-time@mail-tester.com>
```

Every test send is recorded in `test_sends` with a template hash. The scheduler refuses to
send from a mailbox that has never passed one, and refuses to start a campaign whose
templates changed since.

---

## Daily commands

### Render one email exactly as it would send

```bash
.venv/bin/python -m scripts.outbound render --step step1_initial
.venv/bin/python -m scripts.outbound render --step step1_initial --email ada@northwindlabs.test
```

Without `--email` it uses a fixture contact — useful for checking copy changes fast.

### Ask who gets copied, and why

```bash
.venv/bin/python -m scripts.outbound cc-resolve --domain target.test --campaign default --step step3_breakup
```

Prints the resolved lists *and the rule that won*. Use it whenever a CC looks wrong
rather than reading `cc.yaml` and guessing.

### Suppress someone, permanently and globally

```bash
.venv/bin/python -m scripts.outbound suppress add someone@company.com --kind email --reason "unsubscribed"
.venv/bin/python -m scripts.outbound suppress add company.com --kind domain --reason "hard bounce"
.venv/bin/python -m scripts.outbound suppress add "Northwind Labs" --kind company --reason "replied: not interested"
.venv/bin/python -m scripts.outbound suppress list
```

Company suppression stops every contact there, cancels their queued mail, and writes to
`config/suppression.csv` so suppression survives losing the database.

### Load discovery output

```bash
.venv/bin/python -m scripts.outbound ingest --dry-run    # validate everything, write nothing
.venv/bin/python -m scripts.outbound ingest
```

Reads `state/candidates/*.json`. The summary tells you what was added, what was dropped
and why, and **which companies came back degraded** because a research subagent ran out
of search budget — those re-queue rather than counting as finished.

If a file is rejected, fix the research and re-emit it. Do not edit the JSON to satisfy
the validator; the validator is the thing standing between you and a bounce.

---

## The loop, once it is all built

**Weekly — find companies**

```
/outbound-sourcing discover --mode list --file companies.txt
/outbound-sourcing discover --mode vc --fund "Fund Name"
/outbound-sourcing discover --mode industry --run ./industry-research/<topic>/
```

**Weekly — research people.** Ask in natural language: *"research the next 20 companies"*.
Subagents run in parallel batches with a tool budget, write candidate JSON, and you
ingest it. Check the evidence chains on the first few by hand.

**Weekly — verify and review** (milestones 6–7)

```
outbound verify
outbound review export        # CSV + markdown + 5 fully rendered emails
outbound review import reviewed.csv
```

Nothing queues without an approved row. This gate is not optional and not automatable —
it is the last place a human sees the email before a stranger does.

**Send, by hand**

```
outbound send --dry-run
outbound send --limit 20
```

One mailbox, paced with jitter, stopping at `daily_cap`. Exits when the queue or the limit
is exhausted. No daemon, no pool, no warmup — see the removal table in
`references/setup.md`.

**Replies**

```
outbound replies
outbound replies --check
```

Bounces go to the permanent suppression list. Any reply stops the sequence for that contact
and every other contact at the same company. `--check` lists tracked threads with no
detected reply and their sent date, because reply matching over IMAP fails silently and a
missed reply means emailing someone who already answered.


---

## Where things live

```
SKILL.md            what Claude reads to orchestrate all of this
GETTING-STARTED.md  this file
config/             yours, gitignored
config.example/     the template, committed
scripts/            the deterministic spine
  migrations/       forward-only SQL
  providers/        console today; gmail and smtp next
references/         discovery.md, deliverability.md, schema.md, setup.md
state/
  prospects.db      single source of truth
  candidates/       what discovery subagents write
  cache/            every page a subagent fetched, by URL hash
tests/              88 tests, fixtures only, no network
```

---

## Troubleshooting

**`config directory not found`** — `cp -r config.example config`.

**`unknown key ... did you mean ...`** — exactly what it says; the suggestion is computed
from that model's real field names.

**`missing file .../something.pdf`** — `attachments_root` in `campaign.yaml` plus the
`dir`/`files` in `sequence.yaml` must resolve to files that exist. Checked at config load
so it cannot fail mid-campaign.

**A candidate file is rejected** — the error names the record and the rule. The common
ones are evidence without a URL, an email no evidence grounds, and a personalization line
with no source URL or written as a fragment rather than a sentence.

**`attachment set 'x' is N MB on the wire, over the ... limit`** — base64 inflates by 4/3
and gateways measure the encoded size. The error prints a per-file breakdown and tells you
what dropping the largest file would leave.

There are two ceilings and they are independent on purpose:

| key | when | why |
|---|---|---|
| `max_attachment_bytes` | config load | Hard limit. Raise it to let a heavy set out on a **test** send, which only reaches an address you control. |
| `campaign_max_attachment_bytes` | campaign start | Gates real sending. Raising the ceiling above does not raise this one. |

Fix the files rather than raising either as a matter of habit.

**`CAMPAIGN BLOCKERS`** — the config is structurally valid but something in it must not
reach a stranger, most often a placeholder mailing address. Test sends to yourself still
work and render the placeholder so you can see exactly what would ship.

**`no usable token`** — run `outbound auth --mailbox <id>`.

**`cannot commit - no transaction is active`** — you are on an old build; migrations now
run inside the script. Re-pull and re-run `db migrate`.
