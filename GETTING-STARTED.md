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

**Daily — send** (milestone 9)

```
outbound send --dry-run
outbound send
outbound status
```

The daemon handles caps, warmup, windows, jitter, blackouts, and the circuit breaker. If
the breaker trips, it stays tripped until you `--resume`. Find the bad addresses instead
of raising the threshold.

**Daily — replies** (milestone 10)

```
outbound replies
```

Rules handle bounces, OOO and unsubscribes. Ambiguous ones come to you. Interested
replies get a **draft**, never an auto-send.

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

**`cannot commit - no transaction is active`** — you are on an old build; migrations now
run inside the script. Re-pull and re-run `db migrate`.
