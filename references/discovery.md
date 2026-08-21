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
> GitHub public commit emails, personal academic sites and CVs, company `/team`,
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
