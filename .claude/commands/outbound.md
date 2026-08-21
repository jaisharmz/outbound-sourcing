---
description: Run a full outbound investigation on one company, person, or industry and stop at the review gate
argument-hint: <company | person | --industry "topic"> [--role "title"] [--expand N]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Task
---

# /outbound $ARGUMENTS

Run the whole investigation live, from nothing. No pre-staged data, no
accumulated pool — assume the database may be empty for this target.

`SKILL.md` is the workflow. Read it first if it is not already in context. The
deterministic half is the `outbound` CLI, already on PATH inside the venv; the
judgment half is yours. **Never reimplement a step the CLI already does** — the
gates, the validator, suppression, and the evidence contract live there, and a
shortcut around them is how ungrounded records reach the queue.

## Parse the argument

- Bare name that is a company → company run.
- Bare name that is a person → person run: seed the graph from them, expand.
- `--industry "<topic>"` → find companies first, then pick 1–3 and do a company
  run on each. Say which you picked and why.
- `--role "<title>"` narrows who counts as a target; it does **not** widen the
  ICP. If the role is outside `config/icp.yaml`, say so rather than silently
  including or excluding it.
- `--expand N` expands the top N entry points. Default is 0 — see step 3.

## Open a run, so cost is measured rather than estimated

```
export OUTBOUND_RUN_ID=$(outbound run start "<target>")
```

Every CLI step below accumulates into that run file. Your own WebSearch and
WebFetch calls are invisible to it, so **count them yourself as you go** and
record them before reporting:

```
outbound run log --searches <n> --fetches <n>
outbound run report
```

## Narrate

State each search before you run it and what came back after. When something
yields nothing, show the query that found nothing. The operator must be able to
tell a real zero from a broken run — that distinction is the whole point of the
narration, and a company with no publications and no team page is a legitimate
result, not a failure.

Say what you are skipping and why. "Skipping expansion: 3 entry points, all
already resolved" is useful. Silence is not.

## Steps

**1. Resolve the company.** Pass `--tier` here, before anything else.
```
outbound company-resolve "<name>" --domain <domain> --tier startup --ai-depth builds
```
Routing must be set *before* ingest, because contacts inherit tier and campaign
from the account row. Skip it and they ingest cleanly, then sit invisible to a
campaign-scoped review export and unable to ever send — nothing errors, which is
the worst shape of failure. The command prints a red warning if no campaign was
resolved; do not proceed past it.
Exits non-zero if the company is suppressed or personally excluded — stop there
and say so. If the domain is unknown, find it by search first. Check the company
is still independent: an acquisition changes who the recipient works for and
often kills the domain.

**2. Find entry points.** Breadth of source beats depth on any one:
```
outbound traverse-company "<name>"
```
for people who named the company as their own affiliation on a paper, plus:
- the company's own team / research / about pages,
- the widened dork: `site:github.io "<company>"`, `"<company>" research scientist`,
  personal domains, `university.edu/~user` pages,
- arXiv affiliation strings for recent papers.

**Standing rule, non-negotiable:** never conclude a page lacks data from
WebFetch's markdown conversion. Curl the raw HTML and grep for `data-`
attributes and framework JSON before saying a roster is not there. This rule
exists because a 855-company roster was sitting in a `data-companies` attribute
that WebFetch hid.

**3. Expand only where it pays.** Default is not to. On the three companies
measured, a company seed produced its people from the affiliation query, and
one-hop expansion returned the surrounding research community rather than more
employees. Expand when the entry points are few and well-connected, when the
target is a person or lab rather than a company, or when the operator asked.
Say which and why.

**4. Resolve emails, in this order.** Never invent one.
```
outbound person-pages "<Name>" "<Name>" ... --company "<Company>" --json state/pages.json
outbound candidates-from-pages --json state/pages.json --company "<C>" --domain <d> --out state/candidates/<slug>.json
```
**Always pass `--company`.** Without it a guessed URL that lands on a namesake
looks like a clean hit: probing "Pankaj Gupta" for Baseten found a real page
belonging to a different Pankaj Gupta and read a stranger's address off it, and
every other check passed. With `--company` the page must also mention the
employer, and an uncorroborated address comes back as `namesake_risk` rather
than `found`. Never promote one to a contact without confirming it by hand.

Feed URLs you found by search back in with `--url`. Guessing covers the common
shapes but not handles like `amansinghal927.github.io`; on the Together AI run,
search-found URLs recovered two addresses that guessing missed. Search and
guessing are complementary, and using only one leaves addresses on the table.

`candidates-from-pages` writes the address-grounding evidence for you. Do not
hand-write it — on the first run of this command the agent that had just done
the probe forgot that evidence entry and the validator rejected all five
records. Title and personalization are left blank on purpose; those are yours.
1. Observed on a page — first-party, best.
2. A GitHub-learned domain pattern (`outbound harvest-github`) — inferred, and
   must be marked `inferred_from_pattern` with the sample count.
3. Nothing. Below `icp.min_confidence`, leave the person out. A wrong address
   costs a bounce and the operator's sender reputation; a missing one costs a
   name.

**5. Filter.** Write candidate JSON to `state/candidates/<slug>.json` and:
```
outbound ingest --dir state/candidates
```
Ingest applies ICP, suppression, personal exclusions, the per-lab cap, free-mail
and duplicate rules. Report what it dropped and why — the drops are findings.
Then `outbound exclusions` and `outbound verify`.

**6. Personalization must be grounded or null.** One specific thing from a named
source URL, in the operator's voice: plain, technical, no flattery. If you have
nothing specific, set it to `null` — the template has a clean path for that, and
a generic compliment is worse than none.

**7. Stop at the review gate.**
```
outbound review export --campaign <campaign> --out review.md
```
Show the rows and the rendered previews. **Do not send.** The operator approves
with `outbound review import --file review.csv`, then runs `outbound send`.

## Known costs, for calibration

Measured 2026-08-21, both from an empty database:

| | entry points | addresses | searches | fetches | API | wall |
| --- | --- | --- | --- | --- | --- | --- |
| Together AI | 39 | 7 | 2 | 247 | 1 | 5.2 min |
| Baseten | 1 | 0 | 2 | 44 | 1 | 1.5 min |

A company that publishes costs about five minutes; one that does not costs about
ninety seconds and correctly returns nothing. Fetches dominate, and they scale
with the number of names probed (roughly seven URL guesses each), not with the
company's size.

## Close with a cost report

```
outbound run log --searches <n> --fetches <n>
outbound run report
```

Then summarise: entry points found, addresses resolved, contacts queued, and the
conversion between them. If the conversion was poor, say where the loss was —
no personal page, page with no address, name too common, pattern unknown. That
breakdown is the thing the operator is trying to drive down.
