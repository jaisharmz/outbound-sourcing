# Discovery: the research brief and the evidence standard

Finding a person and their email is a research problem, not a scraping problem. It needs
someone to read a lab page, notice that "Members of Technical Staff" sits at a different
URL than "Team", follow a personal site to a CV PDF, and work out that a company uses
`first@` from a single arXiv footnote. That is the agentic half of this system.

## The brief is generated, never hardcoded

Build each subagent's brief from three inputs at runtime:

1. `config/icp.yaml` — who counts as a target.
2. `config/dorks.yaml` — search seeds.
3. The company record from `accounts`.

Plus the live candidate schema (`python -m scripts.candidates`) and the tool budget from
`campaign.yaml`. If you find yourself typing a company name or a job title into a script,
it belongs in config.

## Brief template

> You are researching **{company}** ({domain}) to find people worth contacting about a
> research collaboration.
>
> **Who counts.** Titles: {icp.titles}. Exclude: {icp.title_excludes}. At most
> {icp.max_contacts_per_company} people. Skip anyone whose country is in
> {icp.exclude_regions}.
>
> **Budget: {n} tool calls.** Track them. If you run out, say so — set
> `budget_exhausted: true` and report `searches_used`. A thin answer labelled thin is
> useful. A thin answer that looks complete is worse than nothing.
>
> **Search seeds** (starting points, improvise beyond them): {dorks rendered with the
> company name}
>
> **Sources that work**, because they give you a name and a real email in the same
> document: arXiv PDFs (author emails in the header), Semantic Scholar / OpenAlex,
> personal academic sites and CVs, company `/team`,
> `/research`, `/about`, `/people` pages.
>
> **LinkedIn: search-result snippets only.** Read names and titles off the SERP. Do not
> fetch, crawl, or automate linkedin.com. Resolve names you find there to emails through
> the other channels.
>
> **Evidence.** Every record needs an evidence item grounding the name/title/company
> binding and one grounding the email, each with an absolute URL and a real quote. A
> personalization line needs its own source URL and must be a complete sentence. If you
> cannot ground a detail about someone's work, set `personalization: null`. That is the
> right answer, not a failure.
>
> **When a page returns suspiciously little, check the raw HTML before concluding it
> has nothing.** WebFetch converts to markdown first, and anything the conversion drops is
> invisible to you and indistinguishable from absent. A team page that renders to three
> names when the company clearly has thirty, a roster that looks stale, an empty result
> that contradicts the site — `curl` the source and grep for `data-` attributes and
> framework JSON payloads before you write it off. A plausible wrong answer is worse than
> an error, because nothing downstream catches it.
>
> **Check `known_people` before searching for names.** Some accounts already have founders
> recorded from a fund portfolio at zero search cost. Your budget is better spent resolving
> their email than rediscovering who they are.
>
> **Write** `state/candidates/{slug}.json` against this schema: {schema}. Then stop.
>
> **If you find nobody**, write the file with an empty `candidates` list and a `reason`
> explaining what you looked at. Never pad.

## Harvesting addresses from public commits

**Scope note.** GitHub is a pattern source, not a people source. Harvesting commit
addresses to find *who* to contact was tried and abandoned: a commit proves an address
existed when the commit landed, not that the person is still there or who they are. The
channel that replaced it is personal and lab pages, which carry name, title, research
area and often an address in one first-party document — see `scripts/person_pages.py`.

`outbound harvest-github` is the deterministic half of pattern inference. It resolves a
company's GitHub org, reads author emails off recent public commits, and infers the
domain's local-part convention. No model, no judgment.

Three things it is careful about, each because the naive version misleads:

- **Throttling is not absence.** An unauthenticated probe once reported "no public repos"
  for 78 of 88 companies. That read as a finding and was a rate limit. Every outcome is a
  named status and `throttled` is never reported as `no_public_repos`.
- **A commit proves an address existed when it landed**, not that the person is still
  there. Commit dates are recorded and anything older than ~2 years is pattern evidence
  only, never a sendable contact.
- **Bots and noreply addresses are filtered** before anything can treat them as candidates.

It needs `GITHUB_TOKEN` in `secrets.env` — a fine-grained token with no scopes selected,
since public read is the default. Unauthenticated is 60 requests an hour, which cannot
cover a real roster.

## Email pattern inference

This is the part that pays for the whole exercise. One observed address at a domain
usually gives you the rest of the team.

Find one real address in a document that also names its owner — an arXiv header, a git
commit, a CV. Derive the pattern (`first@`, `first.last@`, `flast@`, `firstl@`). Apply it
to other confirmed names at that domain and mark those `email_basis:
"inferred_from_pattern"`.

An inferred address still needs grounding: the evidence item states the pattern, names
the domain, and links the document the pattern came from. Verification decides whether it
is real; the evidence chain records why you believed it. Never infer from a pattern you
saw only once at a large company, and never mix patterns across subdomains.

## Required check: never conclude "no data" from a converted page

**WebFetch renders a page to markdown before you see it. Anything the markdown
conversion drops is invisible to you, and it looks identical to the page not having the
data at all.** That failure mode has now produced three wrong answers in this project,
every one of them plausible rather than an error:

| what happened | what it looked like | what was true |
|---|---|---|
| a landscape `url` pointed at an arXiv abstract | a clean domain for the company | the domain was `arxiv.org`, and mail would have gone to a stranger |
| one malformed YAML record inside a valid block | the run had no company data | 40 of 41 companies were there |
| a fund's portfolio grid is client-rendered | 21 stale exits, "no current investments" | 855 companies sat in a `data-companies` HTML attribute |

So, whenever a fetch returns suspiciously little — a page you expect to be a roster and it
has a handful of entries, a list that looks stale, a "no results" that contradicts what the
site advertises — **do not conclude anything until you have looked at the raw HTML.**

```bash
curl -sL -A "Mozilla/5.0" "<url>" -o page.html && wc -c page.html
grep -oE 'data-[a-z-]+="\[' page.html | sort -u          # embedded JSON in attributes
grep -c '__NEXT_DATA__\|application/json' page.html      # framework payloads
grep -oE '"[a-z_]*(name|company|title)"\s*:' page.html | sort -u | head
```

Check `data-` attributes specifically. A 3.6 MB page that renders to two screens of
markdown is telling you the content is in the source and not in the conversion.

The general rule this is an instance of: **a source that returns a plausible wrong answer
is more dangerous than one that errors**, because nothing downstream flags it. When a
result is thinner than the source should be able to produce, treat that as a signal to look
harder rather than as a finding.

## OpenAlex: read `affiliations`, never `last_known_institutions[0]`

Worth its own note because getting it wrong produced a confident, plausible,
completely incorrect answer — and then a wrong conclusion *about* the source.

`last_known_institutions` is a list whose order is not recency. Taking `[0]` gives
"Berkeley College" for a Together AI researcher and "BioQ Pharma" for a Stanford one.
On that reading the field looks like bad data, and the natural conclusion is that the
source cannot be trusted for affiliations at all. That conclusion was wrong.

`affiliations[]` carries each institution with the years it was observed. Ranked on
**sustained recency** — how many of the last three years, not just the newest — it
resolves correctly in 6–7 of 8 spot checks, and the failures arrive with low confidence
rather than confidently wrong. A single recent year next to a multi-year run is usually a
parsing artefact: "Berkeley College [2026]" beside "Together [2026, 2025, 2024]".

One real defect remains: **OpenAlex conflates some distinct people into one author
record.** "Junxiong Wang" carries 198 works and 8,308 citations and resolves at high
confidence while being at least two researchers. The tell is a works count wildly out of
line with career stage — a second-year PhD student does not have 198 papers.

The general lesson, which is the one that keeps recurring: when a structured source looks
like bad data, check whether you are reading the wrong field before concluding the source
is unreliable. A wrong conclusion about a source is more expensive than a wrong value,
because it makes you discard something that works.

## Caching

Cache every fetch to `state/cache/`, keyed by URL hash. Re-runs get cheaper, and the user
can see exactly what a subagent read — which is the only way to audit a claim after the
fact.

## The failure mode to design against

`industry-research`'s own SKILL.md documents this, and it has happened in production:
WebSearch is capped per session. A run that exhausts the cap can still fetch any page
whose address it already has, so grounding and verification keep working and the output
keeps looking complete — while discovery has stopped entirely. Companies nobody mentioned
never get found. The roster comes back accurate about what it contains and quietly short.

Three defences, all cheap:

1. Report `searches_used` and `budget_exhausted` in every file, honestly.
2. Ingest stores a budget-exhausted company as `degraded`, not `done`, so it re-queues.
3. Say it in the run summary. A degraded run is worth shipping. A degraded run that
   reads as complete is not.

## Every company goes through the identical process

No special-casing. A company you happen to know well gets the same brief, the same
budget, and the same evidence standard as one you have never heard of — otherwise the
quality of the output tracks your prior familiarity rather than what is actually findable.

## Design intent: best-first traversal (not built)

The traversal currently expands breadth-first from seeds and scores afterwards.
The intended end state is best-first: `graph.score_node` becomes the key of a
priority queue that *drives* expansion rather than ranking its output, so budget
goes to the most promising frontier node at each step instead of being spread
evenly. The scoring function already exists and already combines the right
signals -- `path_count` (proximity), recency, topic overlap against seed topics,
reachability, seniority -- so this is mostly plumbing: replace the expansion
loop's queue with a heap, re-score the frontier after each expansion, and stop
on marginal-yield collapse rather than on a hop limit.

**Deliberately deferred, and the reason is a measurement rather than a
preference.** The run on Together AI, Fireworks AI and Baseten (2026-08-21)
found that Together's 39 entry points came from the affiliation query, not from
the graph, and that one-hop expansion returned the surrounding research
community rather than more people at the company. Fireworks returned 3 and
Baseten 1 because they barely publish. A better traversal *policy* does not help
a graph with nothing to traverse -- it allocates a budget better across a
frontier that is either already exhausted or full of the wrong people.

Revisit when seeds are labs and universities rather than companies. There the
frontier is genuinely large, publication density is high, and expansion order
starts to matter.

## The number to attack next: contactability

The same run: **39 entry points at Together AI produced 5 findable addresses,
13%.** That loss is larger than anything better sourcing can recover -- doubling
entry points at a 13% conversion is worth less than moving 13% to 30%.

The breakdown of the 34 misses is the thing to measure, because each bucket has
a different fix and they are not equally tractable:

| bucket | likely fix |
| --- | --- |
| no personal page found | more URL shapes; lab pages; institutional directories |
| page found, no address on it | read the paper PDF's first page, where affiliation emails live |
| name too common to resolve | needs an id-based lookup, not a guessed URL |
| pattern unknown for the domain | GitHub domain-pattern learning, already built |

`person_pages.find` already records `tried` and returns a status that separates
`not_found` from `no_email`, so the first two buckets are countable now without
new instrumentation.


## Plausible wrong answers

The recurring failure in this system is not a broken result, which is obvious and
gets fixed. It is a **well-formed, confident, wrong** one, which survives review
because there is nothing on its surface to object to.

### Cursor Insight Ltd. — a real company with the wrong name

Seeding a company from OpenAlex affiliation strings matches the company name
against what authors typed about themselves. `Cursor` returned 25 people. Two
guards were added and both worked as designed:

- **founding year** — Cursor (Anysphere) was founded in 2022, so pre-2022
  matches are the ordinary English word. This removed Spanish ecology prose from
  1990 that happened to contain "cursor".
- **prose test** — a real affiliation is a short comma-delimited address
  (`Groq, Inc, Palo Alto, CA, USA`); a biography is a sentence. This removed
  "Kam L. Wong is Vice President of Kambea Industries…", which is how `Etched`
  matched the *verb* in semiconductor abstracts back to 1991.

Eight people passed both. All eight work at **Cursor Insight Ltd.**, a London
handwriting-analytics firm: `Cursor Insight Ltd., 20-22 Wenlock Road, N17GU
London, United Kingdom`. Recent, correctly formatted, genuinely first-party, and
a completely different company.

**No heuristic separates them.** The affiliation line is not defective in any
way a rule can detect — it is a correct answer to the question "who says they
work somewhere called Cursor". Only knowing that two companies share a name
resolves it, so the fix is an explicit per-company exclusion (`EXCLUDE_PATTERNS`)
rather than a smarter test.

**Why this matters beyond Cursor.** The review gate cannot catch this either. A
reviewer sees a real name, a real company, a real address and a real citation.
Everything checks out, because everything *is* true -- about a different
organisation. The same shape recurs:

| the plausible wrong answer | what made it wrong | what caught it |
| --- | --- | --- |
| Cursor Insight Ltd. | different company, same name | knowing they differ; nothing else |
| Kaitlyn Zhou "at Together AI" | an invited talk, not employment | reading the sentence around the match |
| OpenAlex `last_known_institutions[0]` | an unordered list read as ranked | checking the field's contract |
| a16z portfolio "no current investments" | 855 companies in a `data-` attribute | curling raw HTML |
| `hate@spam.net` on a real page | a trap planted for scrapers | a denylist |
| `web@zvfh.dev` | a role account, not a person | a prefix rule |

The common defence is not a better heuristic. It is **checking the claim against
a second, independent source** before it becomes a contact -- and where that is
impossible, encoding the knowledge explicitly and saying so in the code.

## Channels by population

The personal-page channel is not universal. Measured 2026-08-21:

| population | example | personal page with an address | what works instead |
| --- | --- | --- | --- |
| academically-publishing ML research | Together AI | 13 of 42 | personal pages (github.io, custom domains) |
| systems and hardware | Groq | 2 of 72, both unusable | **paper first pages** |
| non-publishing product companies | Baseten, Fireworks | 0 | neither; needs another channel entirely |

Groq had 72 entry points and 48 of them had no page at all. Its people publish at
ISCA/MICRO/ASPLOS rather than blogging, and those papers print author emails at
the company domain under the title. `outbound paper-emails Groq --domain
groq.com` recovered 8 named, first-party `@groq.com` addresses in about 20
seconds from a single paper, and measured the domain convention
(`first_initial_last`, 7 samples) which then applies to the other 64 people.

Pick the channel from the population, not the other way round.
