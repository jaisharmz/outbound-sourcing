# Schema

The interface between the agentic layer and the deterministic one, and the database
everything downstream reads.

## The two-layer contract

Discovery writes exactly one artifact: `state/candidates/<company-slug>.json`. Nothing
else. `scripts/ingest_candidates.py` validates it against the Pydantic models in
`scripts/candidates.py` and loads it into SQLite. No script after that point reads a
model's output.

A file that fails validation is rejected **whole**. A record that got halfway into the
database is worse than one that never arrived, because the half that landed looks
indistinguishable from a verified one.

## Candidate file

```json
{
  "company": "Northwind Labs",
  "domain": "northwindlabs.test",
  "generated_at": "2026-08-20T17:00:00Z",
  "searches_used": 9,
  "tool_calls_used": 12,
  "budget_exhausted": false,
  "reason": null,
  "candidates": [ ... ]
}
```

| Field | Meaning |
|---|---|
| `searches_used` | How many web searches this subagent spent. |
| `budget_exhausted` | True if it hit the cap. Stores the company as `degraded`, which re-queues it. |
| `reason` | **Required when `candidates` is empty.** An unexplained empty file is indistinguishable from a crashed subagent. |

Generate the live schema with `python -m scripts.candidates`, and paste it into the
research brief so a subagent writes against the real thing rather than a paraphrase.

## Candidate record

```json
{
  "name": "Ada Lovelace",
  "title": "Research Scientist",
  "company": "Northwind Labs",
  "email": "ada@northwindlabs.test",
  "email_basis": "observed",
  "evidence": [
    {"claim": "Ada Lovelace works at Northwind Labs as a Research Scientist",
     "url": "https://northwindlabs.test/team",
     "quote": "Ada Lovelace — Research Scientist, sequence models",
     "retrieved_at": "2026-08-20T16:41:00Z"},
    {"claim": "email is ada@northwindlabs.test",
     "url": "https://arxiv.org/abs/0000.00001",
     "quote": "Ada Lovelace (ada@northwindlabs.test), Northwind Labs",
     "retrieved_at": "2026-08-20T16:44:00Z"}
  ],
  "personalization": "I came across your work on long-context retrieval, and the caching ablation in section 4 was the part I keep thinking about.",
  "personalization_source_url": "https://arxiv.org/abs/0000.00001",
  "confidence": 0.91,
  "country": "US",
  "timezone": "America/Los_Angeles"
}
```

Optional: `country`, `timezone`, `linkedin_url`. Anything else is rejected as an unknown
field — the schema is closed on purpose.

## What the validator enforces

These are structural. They are not things a subagent is asked nicely to do.

1. **At least one evidence item, each with an absolute http(s) URL and a real quote.**
   `"..."`, `"N/A"` and `"-"` are rejected as quotes.
2. **The identity binding is grounded.** Some evidence item must name the company *as
   whole tokens* and carry an identity phrase (`works at`, `is a`, `role`, `title`,
   `affiliation`, `member of`). Company names are normalized first, so evidence naming
   "Kepler Systems" grounds a record whose company is "Kepler Systems, Inc." — but
   evidence that merely contains the string `northwindlabs.test` does **not** ground a
   binding to "Northwind Labs". Those are different claims.
3. **The email is grounded.** Either an evidence item contains the address, or — for
   `inferred_from_pattern` — an item states the pattern and names the domain, with a URL.
4. **Personalization is sourced and is a sentence.** Non-null personalization requires
   `personalization_source_url`, and must start with a capital and end in `.`/`!`/`?`.
   Templates drop it in as its own paragraph and cannot fix grammar.
5. **The email parses**, and is not on the suppression list.

When grounding is not there, emit `personalization: null`. The template falls back
cleanly and the email still reads as written by a person.

## Ingest-time filtering

Deterministic, applied after validation, all reported in the ingest summary:

- suppression (email, domain, company — checked again here even though discovery
  already checked)
- free-mail addresses
- same person seen twice at one registrable domain
- ICP rules from `icp.yaml`: title match, title exclusions, minimum confidence,
  excluded domains and companies, `exclude_regions`
- `max_contacts_per_company`

## Database

`state/prospects.db`. Migrations in `scripts/migrations/`, forward-only, each applied in
a single transaction.

| Table | Holds |
|---|---|
| `accounts` | Companies, their discovery source, and `status`: `new`, `researching`, `done`, `degraded`, `excluded`. |
| `contacts` | People. Verification status, approval, sequence status. |
| `evidence` | Every claim's URL and quote, kept so the review gate is reviewable. |
| `campaigns`, `enrollments` | Which contact is on which sequence, and when the next step is due. |
| `messages` | One row per email. `queued → sending → sent`, with `idempotency_key`. |
| `replies` | Classification and its source: `rules`, `model`, or `human`. |
| `suppression` | Permanent, global, mirrored to `config/suppression.csv`. |
| `test_sends` | Mailbox, step, template hash. The scheduler's gate. |
| `mailbox_day` | Per-mailbox daily counters, in **messages and recipients**. |
| `circuit_breaker` | One row. Tripped state and reason. |
| `events` | Structured log of everything that changed state. |

### Crash safety

A message row commits as `sending` **before** the provider call and updates to `sent`
after. A crash between the two leaves a `sending` row, which reconciliation resolves
against provider state. `idempotency_key` is `contact:step:template_hash`, so a retry
after a partial failure cannot produce a second email.
