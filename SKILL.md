---
name: outbound-sourcing
description: Source prospective clients and run cold outbound end to end — find companies, research named people and ground their emails in evidence, verify addresses, review before anything sends, then send by hand from a single mailbox and watch for replies. Use when the user says "find me clients", "run outbound", "source companies", "who should I email at X", "check replies", "how did the campaign do", or asks to add someone to the suppression list. Discovery is agentic and uses WebSearch/WebFetch/subagents; everything from ingestion onward is scripts with no model in the loop.
argument-hint: <discover|research|verify|review|send|replies|report> [--campaign <name>] [--dry-run]
user-invocable: true
---

# Outbound sourcing

Two layers, and the boundary between them is the whole design.

**Agentic — you, at runtime.** Reading a lab's site and noticing that "Members of
Technical Staff" lives at a different URL than "Team". Following a personal page to a CV
PDF. Inferring that a company uses `first@` from one arXiv footnote. Deciding who at a
company is worth contacting. Writing the personalization line. This is research, and it
is your job, not a scraper's.

**Deterministic — scripts, no model.** Domain resolution, dedupe, ICP filtering, MX/SMTP
verification, template rendering, CC resolution, pacing, jitter, caps, blackouts, the
send itself, retries, state transitions, suppression, and bounce tracking.

The send path contains zero model calls. `tests/test_send_path_purity.py` enforces that
by walking the import graph — it is checked, not promised.

## The contract

Agentic discovery writes **only** to `state/candidates/<company>.json`.
`scripts/ingest_candidates.py` validates and loads it. Everything downstream reads
SQLite and never reads you.

Every record carries evidence with URLs. The validator rejects any record where the
name/title/company binding or the email lacks a URL, and any record whose
`personalization` has no `personalization_source_url`. At 500 sends/day a hallucinated
contact is not a bug anyone notices in time — it is a bounce, and enough of them cost
the sending domain permanently.

**If you cannot find grounding, emit `personalization: null` and let the template fall
back.** That is always the correct move over inventing a detail about someone's work.

See `references/schema.md` for the full schema and `references/discovery.md` for the
research brief and the evidence standard.

## Setup

First run, or a new machine: read `GETTING-STARTED.md`. Short version:

```bash
cd ~/.claude/skills/outbound-sourcing
uv venv --python 3.13 && uv pip install -e ".[dev]"
cp -r config.example config          # then edit config/
python -m scripts.outbound validate-config
python -m scripts.outbound db migrate
```

`config/` is gitignored and holds everything user-specific. Nothing about a particular
user belongs in `SKILL.md`, `scripts/`, or `references/`.

## Commands

Every script runs as `python -m scripts.<name>`; this file orchestrates rather than
reimplements. Run them from the skill directory with the venv active.

| Command | What it does |
|---|---|
| `outbound validate-config` | Load and cross-check every config file. Run after any edit. |
| `outbound db migrate` \| `db stats` | Schema, row counts. |
| `outbound ingest [--dry-run]` | Validate candidate JSON into SQLite. |
| `outbound render --step <id> [--email <addr>]` | Render one email exactly as it would send. |
| `outbound cc-resolve --domain --campaign --step` | Show which CC rule wins and why. |
| `outbound suppress add <value> --kind email\|domain\|company` | Permanent, global. |
| `outbound demo` | End-to-end on fixtures through the console mailbox. No network, scratch DB. |
| `outbound harvest-github --prefilter pass_builds` | Read addresses off public commits, infer each domain's email pattern. Needs `GITHUB_TOKEN`. |
| `outbound auth --mailbox <id>` | OAuth for one mailbox. Names the exact failure mode. |
| `outbound test-email --mailbox <id> --step <id>` \| `--all-mailboxes` \| `--to <addr>` | Real email to `test_recipient`, then prints outgoing **and delivered** headers with SPF/DKIM/DMARC verdicts. |

`--to` is the only path in the system that reaches an address which never passed the
review gate, so it is gated: the address must be `test_recipient` or match
`campaign.yaml: test_send_allowlist` (exact addresses or `*@domain`, subdomains included),
otherwise `--force` is required and prints a loud warning. **Suppression is checked
regardless and `--force` does not override it** — an opt-out is permanent and global, and
"it was only a test" is not an exception the recipient agreed to. Every outcome, including
every refusal, is recorded in `test_sends` with `allowlisted` and `forced` flags.

Milestones 4 onward add: `discover`, `verify`, `review`, `send_queue`, `watch_replies`,
`report`. This table grows with them.

**Two gates block a campaign and are not to be worked around.** `validate-config` exits
non-zero while any campaign blocker is unresolved — a placeholder mailing address is one,
since CAN-SPAM requires a real one and the footer ships on every template. And attachment size is
capped twice, in wire bytes: `max_attachment_bytes` hard-fails at config load, while
`campaign_max_attachment_bytes` gates a campaign start separately — so a ceiling loosened
to let a heavy set out on a test send cannot leak into real sending. An oversized set
bounces for reasons unrelated to address quality, which is true at twenty sends a day
exactly as it is at five hundred.

## Running discovery

### 1. Companies

```bash
outbound discover --mode list     --file companies.txt --tier startup
outbound discover --mode vc       --file portfolio.txt --tier startup
outbound discover --mode industry --run  ./industry-research/<topic>/
outbound accounts --needs-domain          # what is blocking people discovery
```

All three land in `accounts`. The industry adapter reads `landscape.md`'s fenced YAML
block, not the prose and not the avenue frontmatter — see `references/schema.md` for the
shape and for the three ways it drifts.

**A landscape `url` is evidence, not a homepage.** Google DeepMind's is an arXiv abstract.
Never take a sending domain from one without checking it against the aggregator lists;
derived domains are stored as candidates and still need resolution.

An `industry-research` run costs 20–60 minutes and most of a session's search budget, so
it is an occasional source. Companies persist in SQLite; the daily loop reads from there.

### Campaigns

Accounts enroll into a campaign by tier, per `config/campaigns.yaml`. Two segments that
fail for different reasons do not share copy: a small company ignores you because nobody
read it, a large lab ignores you because the named researcher has no mechanism to engage
an outside group without a formal partnership process. A blended reply rate hides which
one is working, so tier and campaign are carried to the contact record and reporting
breaks them out separately.

Campaign templates live in `templates/<campaign>/` and fall back per file to `templates/`,
so a campaign only overrides the copy it actually changes. **A template still containing a
bracketed placeholder blocks its campaign** — an unwritten email must not be sendable.

### 2. People — the agentic loop

For each company, spawn a subagent with a research brief and a tool budget (default 15,
from `campaign.yaml`). Run companies in parallel batches. **The brief is generated from
`config/icp.yaml` + `config/dorks.yaml` + the company record — never hardcoded.** Build
it per `references/discovery.md`.

The subagent's job: find people matching the ICP, find or infer their emails, ground
every claim, write the JSON, stop. Tell it the ICP, the evidence requirements, the
budget, and what to do when it finds nothing — emit an empty file with a `reason`, never
pad.

Sources that give you a name and a real email **in the same document**, which is what
makes pattern inference work: arXiv PDFs (emails in the header), Semantic Scholar /
OpenAlex, GitHub public commit emails, personal academic sites, and company `/team`,
`/research`, `/about` pages.

`config/dorks.yaml` holds search *seeds*, not a script. Improvise beyond them.

**LinkedIn: SERP snippets only.** Read names and titles off search results. Never fetch,
crawl, or automate anything on linkedin.com — it breaks their ToS and gets the user's IP
blocked. Names found there get resolved to emails through the other channels.

Cache every fetch to `state/cache/` keyed by URL hash, so re-runs are free and the user
can see what a subagent actually read.

**Report your search budget honestly.** Set `searches_used` and `budget_exhausted` in
every candidate file. A run that exhausts WebSearch keeps fetching pages it already has
addresses for, so verification still works and the output still looks complete while
discovery has silently stopped. A company marked `budget_exhausted` is stored as
`degraded`, not `done`, and re-queues. Never let a thin run read as a finished one.

### 2b. What a good run looks like when it finds nothing

Two of the first three pilot companies emitted an empty file, and both were correct:

- **An acquisition invalidated the affiliation.** Anyscale's three portfolio-listed
  founders are all real and findable, but the company was acquired three weeks before the
  run and the whole team is moving. Writing contacts there would have meant asserting a
  current affiliation we cannot support — and the fund still listed the company `Active`.
- **Names were groundable, emails were not.** Udio's five founders are confirmed in a
  launch announcement, but no `@udio.com` address is observable anywhere: not on the site's
  raw HTML, not in an arXiv footnote, not in Crossref. With no observed address at the
  domain there is nothing to infer a pattern from, and inferring one anyway is a guess
  dressed as a record.

Both are stored as `no_contacts`, not `done`, so they re-queue when something changes.
A file whose `reason` explains itself is a successful run.

### 3. Ingest

```bash
python -m scripts.outbound ingest --dry-run   # validate first
python -m scripts.outbound ingest
```

A file that fails validation is rejected whole. Read the error, fix the research, re-emit
the file. Do not edit the JSON to make the validator pass — the validator is the point.

## The human review gate

Hard stop between enrichment and queueing. Nothing queues without an approved row.
Export CSV plus a readable markdown table with name, title, company, email, verification
status, confidence, evidence URLs, personalization and its source URL — and **five fully
rendered emails, CC line included**. The user edits the `approved` column and re-imports.

## Sending

Nothing about sending involves you. Sends are a foreground command the operator invokes:

```bash
outbound send --dry-run
outbound send --limit 20
```

It paces with the configured jitter, respects the sending window and `blackout_dates.yaml`,
stops at `campaign.yaml: daily_cap`, and exits when the queue or the limit is exhausted.
It refuses to send from a mailbox that has never passed a test send, and refuses to start
a campaign whose template hash differs from the one on that test send.

Crash safety is unchanged: a message commits `sending` **before** the provider call and
`sent` after, so a crash never double-sends.

**Scope.** One mailbox at 15–25 sends a day. There is no pool, no warmup ramp, no daemon
and no circuit breaker — see the removal table in `references/setup.md` for what went and
why. The evidence contract, the review gate, the attachment gates, bounce suppression and
crash safety all stayed, because none of them depend on volume.

## Replies

Any reply immediately stops the sequence for that contact **and every other contact at the
same company**. A deterministic rules pass over headers detects bounces and writes them to
the permanent suppression list.

Classification and draft generation are gone — the operator reads their own inbox at this
volume. What matters instead is knowing when reply *detection* fails, since a missed reply
means emailing someone who already answered:

- every inbound message that matches no tracked thread is recorded in `unmatched_inbound`
  rather than discarded
- `outbound replies --check` lists tracked threads with no detected reply alongside their
  sent date, so stale ones can be eyeballed against the real inbox

Matching runs over IMAP on `In-Reply-To`/`References` against the Message-ID we generate
ourselves, which is the weaker of the two possible mechanisms and the reason both of the
above exist.

## Never

- Build a crawler, headless browser, or SERP scraper — WebSearch, WebFetch and subagents
  are the crawler.
- Fetch or automate linkedin.com.
- Put persona strings in `scripts/` or `references/`.
- Call a model anywhere in the sending path.
- Send to an unverified address, or one whose evidence chain is incomplete.
- Emit a candidate record with an ungrounded claim.
- Silently retry an ambiguous send failure.
- Pad a thin company with guesses to make a run look productive.

## References

- `GETTING-STARTED.md` — install, configure, and the daily/weekly loop.
- `references/discovery.md` — the research brief and the evidence standard.
- `references/schema.md` — candidate schema, DB schema, the two-layer contract.
- `references/deliverability.md` — CC accounting, attachment size, what was removed.
- `references/setup.md` — install, the single mailbox, and the removal table.
