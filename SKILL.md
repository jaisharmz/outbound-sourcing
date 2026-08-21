---
name: outbound-sourcing
description: Find named people at a company, ground every claim about them in a source you can open, and leave a personalized email as a draft in the operator's Gmail for them to read and send by hand. Use when the user says "find me clients", "run outbound", "source companies", "who should I email at X", asks to draft outreach to a company or field, or asks to add someone to the suppression list. Discovery is agentic and uses WebSearch/WebFetch; everything from ingestion onward is scripts with no model in the loop.
argument-hint: <company | person | --industry "topic">
user-invocable: true
---

# Outbound sourcing

Finds people worth emailing, proves who they are, and leaves drafts in Gmail.
**It never sends.** The operator opens the inbox and sends each one by hand.

## Two layers

**Agentic — you, at runtime.** Reading a lab's site and noticing that "Members of
Technical Staff" lives at a different URL than "Team". Following a personal page to
a CV. Deciding who at a company is worth contacting. Writing the personalization
line. This is research, and it is your job, not a scraper's.

**Deterministic — scripts, no model.** Dedupe, ICP filtering, suppression,
verification, rendering, size gates, drafting, state transitions.

The send path contains zero model calls. `tests/test_send_path_purity.py` walks the
import graph and fails if it ever could — checked, not promised.

## Start here: `outbound investigate`

    outbound investigate "<company>" --domain <domain>

Discovery is an **investigation loop**, not a sequence of channels. Every channel
here fails on some population: personal pages fail on hardware engineers, paper
first pages fail on companies that do not publish, GitHub patterns fail without a
public org, rosters fail where no org exists. Running them in a fixed order and
reporting what each missed produces an accurate list of gaps and very few contacts.

The loop asks one question per step: **what is the next investigation that gets me
closer to a grounded contact?** A partial result is a lead, not a dead end.

| what you have | what it is a lead to |
| --- | --- |
| a personal page with no address | its Scholar link, or the person's papers |
| a Scholar profile | the papers it lists |
| a paper carrying company addresses | the addresses, **and every coauthor** |
| a name and no address | the domain's convention, learned from commits |
| an address and no role | the roster, the team page, their own page |

One entry point becomes a team, and each record rests on a dated primary document
naming both the person and the employer.

**Stopping:** `--budget` steps, or `--max-dry` consecutive steps yielding neither a
fact nor a lead. Both are needed — budget alone lets one rich seed spend everything
on a single company; dryness alone never terminates on a coauthor graph.

Every step is logged to `state/investigations/`. The reasoning is the reviewable
part: a contact whose derivation cannot be read is a contact that cannot be checked.

## Which channel suits which population

This determines whether the tool works at all for a given target. Measured
2026-08-21.

| population | example | personal pages | what works |
| --- | --- | --- | --- |
| publishing ML researchers | Together AI | 13 of 42 | personal pages, OpenAlex |
| systems and hardware | Groq | 2 of 72, both unusable | paper first pages, commit patterns |
| non-publishing product companies | Baseten, Fireworks | ~1% | GitHub org + commit patterns |
| open-source projects | LangChain, vLLM | nothing | **nothing — contributors commit from personal addresses, so there is no domain convention to learn** |

Pointing this at four open-source projects and getting zero is the tool working
correctly, not a failure. Say so rather than padding the run.

## The evidence contract

Agentic discovery writes **only** `state/candidates/<company>.json`.
`scripts/ingest_candidates.py` validates and loads it. Everything downstream reads
SQLite and never reads you.

Every record carries evidence with URLs. The validator rejects any record where the
name/title/company binding or the email lacks a URL, and any record whose
`personalization` has no `personalization_source_url`.

**An address is either observed or inferred, and they are different claims.**
Observed means seen on a page, in a paper, or in a commit. Inferred means a domain
convention predicts it — applied only above 90% confidence and 5 samples, marked
`inferred_from_pattern`, and flagged at review as NOT OBSERVED, because that is the
one that bounces.

**If you cannot find grounding, emit `personalization: null`** and let the template
fall back. Always correct over inventing a detail about someone's work.

**A title is never inferred from activity.** Commit history is evidence about what
someone does, not a claim about their role. An unfound title stays `Unknown` and is
flagged for a per-row decision.

## Gates between a record and a send

Each exists because something failed silently once.

| gate | what it stops |
| --- | --- |
| evidence validator | a claim with no source you can open |
| suppression | anyone who asked to stop, permanently, plus bounces |
| personal exclusions | people the operator already knows, one hop over the graph |
| leadership filter | founders and execs, scanned from the company's own pages |
| review gate | everything, until a human approves it |
| link checker | a linked document behind a login or request-access wall |
| size gates | an attachment set that hard-bounces on corporate gateways |
| template hash | copy edited after the last test send |
| delivered-From check | Gmail rewriting the sender in transit |
| DB guard | anything but the CLI opening the production database |

## Commands

| | |
| --- | --- |
| `outbound investigate "<co>" --domain <d>` | find people, chase evidence |
| `outbound discover --file companies.txt` | load a target list |
| `outbound suggest "<terms>" --pick 1,3` | companies on hand matching an industry |
| `outbound ingest --dir state/candidates` | validate and load candidate files |
| `outbound verify` | MX, then SMTP where the network allows |
| `outbound review export --out review.md` | the human gate |
| `outbound review import --file review.csv` | load approvals |
| `outbound send` | write Gmail drafts. `--send` actually sends |
| `outbound drafts` | what is waiting |
| `outbound mark-sent --all` | force-mark; normally detected automatically |
| `outbound suppress <email>` | honor an opt-out |
| `outbound doctor` | what is missing and how to fix it |

## Never

- Build a crawler, headless browser, or SERP scraper — WebSearch and WebFetch are
  the crawler.
- Fetch or automate linkedin.com. SERP snippets only.
- Put persona strings in `scripts/` or `references/`.
- Call a model anywhere in the sending path.
- Send to an address whose evidence chain is incomplete.
- Infer a title from activity.
- Pad a thin company with guesses to make a run look productive.
- Conclude a page lacks data from WebFetch's markdown conversion. Curl the raw HTML
  and grep for `data-` attributes first — an 855-company roster was once sitting in
  one.

## References

- `references/discovery.md` — the evidence standard, channels by population, and the
  plausible-wrong-answers write-up: real records that were confidently wrong.
- `references/schema.md` — candidate file schema and the database.
- `references/deliverability.md` — attachments, links, CC accounting.
- `references/compliance.md` — why there is no footer, and what the opt-out
  obligation actually is.
- `SETUP.md`, `USAGE.md` — installing and running it.
