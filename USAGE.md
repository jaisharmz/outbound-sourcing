# Usage

## The common path is three commands

```bash
outbound investigate "Baseten" --domain baseten.co    # find people, ground them
outbound review export --out review.md                 # read them
outbound send                                          # write Gmail drafts
```

Then open Gmail, read each draft, send the ones you want, and:

```bash
outbound mark-sent --all
```

Nothing sends itself. `outbound send` writes drafts; `--send` is the real thing
and you almost certainly do not want it yet.

---

## Running a search

**One company:**
```bash
outbound investigate "Together AI" --domain together.ai
```

**Start from a name you already know** — the loop expands outward from them:
```bash
outbound investigate "Together AI" --domain together.ai --seed "Tri Dao"
```

**An industry**, when you do not have a target list:
```bash
outbound suggest "inference, quantization, serving"
outbound suggest "inference, quantization, serving" --pick 1,3,5
```
That prints the `investigate` commands for the ones you chose.

**A list of companies:**
```bash
outbound discover --file companies.txt --tier startup
```
One per line, `Name` or `Name,domain.com`.

---

## The questions it asks

It runs unattended and stops for four things only:

```
Found 4 people at Modal Labs while searching Baseten.
Include Modal Labs?
  y / n / always
```

`always` and `never` settle that class for the rest of the run, so you are not
asked twelve times. The run summary tells you how much they decided:
`standing answers decided: 2 companies included by 'always'`.

Set `autonomy` in `config/campaign.yaml` to change the default:

- `ask` — stop for judgment calls (default)
- `auto` — expand freely, report every decision at the end
- `strict` — never leave the companies you named

Seniority and ambiguity are asked under every setting. They are judgments about
a specific person and a specific claim, not about how wide to search.

---

## Reading the review

```bash
outbound review export --out review.md
```

Rows are sorted best-first: individual contributors before senior ICs before
leadership, with unknown-title rows grouped together at the end.

**`title_status: unknown` is not a bug.** It means the loop looked for a job
title on the company roster, the team page and the person's own site, and did
not find one. It is recorded as unknown rather than guessed, because a title is
a claim about someone's role and commit history is only evidence about their
activity. Decide per row from the evidence shown.

**`NOT OBSERVED` is the flag that matters.** It means the address was never seen
anywhere — it is what the domain's convention predicts. If the convention does
not hold for that person, it bounces. Every other flag asks whether the right
person receives the message; this one asks whether anyone does.

Approve by putting `y` in the `approved` column and:

```bash
outbound review import --file review.csv
```

---

## Sending

`outbound send` writes drafts into Gmail. Open them, read them, send by hand.

**Then run `outbound mark-sent --all`.** This is the one manual step, and
forgetting it breaks things quietly: the contact stays un-contacted, so a later
run can queue them again, reply tracking never starts, and company suppression
never applies. Nothing errors — it just silently behaves as if you never wrote
to them.

**Pacing.** A personal Gmail sending sixty cold emails in one sitting looks like
what it looks like. Fifteen to twenty-five a day is the shape this is built for.
Spread a big batch over several days.

**When someone asks to stop:**
```bash
outbound suppress someone@example.com
```
Permanent, global, checked on every send. Bounces and detected replies suppress
automatically. Honoring the request is the obligation — do it the moment it
arrives.

---

## Running this as a group

Five people sharing a target list will collide, and two emails from one club to
one person in a week reads as disorganised.

Point everyone at one shared file:

```yaml
# config/campaign.yaml
claims_file: ~/club-outbound/claims.csv     # a git repo, Drive, Dropbox
claims_stale_after_days: 28
```

It is an append-only CSV. Pull before a run, push after. Two people claiming
different companies produce two new lines, which git merges without a conflict.

**You do not have to remember to claim anything.** `outbound mark-sent --all`
records both the company and each person automatically — sending *is* the claim.
Use `outbound claim "Baseten"` only when you want to stake one out before you
have written anything.

```
outbound claim "Baseten"       # I'm working on this now
outbound claims                # who has what
outbound claims --stale        # claimed long ago, never worked
```

`/outbound` and `outbound company-resolve` warn if someone else holds a company,
and person-level claims catch the case a company check cannot: two members
reaching the same researcher through different affiliations.

Claims expire after `claims_stale_after_days`. A stale claim is still shown —
"Ada looked at this two months ago" is useful — but it stops reserving anything.

**Set `OUTBOUND_USER`** in your shell if `$USER` is not how the group knows you.

**Your persona is yours.** Everyone has their own `config/`: their own resume,
Drive links and signature. Nothing in `config/` is shared, and the claims file is
the only thing that is.

## Troubleshooting

Run `outbound doctor` first. It diagnoses most of this and prints the fix.

**A company returns nothing.**
Check what kind of company it is. Open-source projects return zero by design —
contributors commit from personal addresses, so there is no company convention
to learn. Product companies without papers need a public GitHub org. If the
investigation log in `state/investigations/` shows steps running and finding
nothing, that is a real zero, not a failure.

**A namesake got through.**
The prober requires the page to mention the company, but two people with the
same name at similar companies can defeat that. If a review row's evidence URL
does not look like the right person, reject it — that is what the gate is for.
Report it and the corroboration can be tightened.

**Links fail the gate.**
`outbound check-links`. Almost always Drive sharing set to "Restricted" instead
of "Anyone with the link", or a `/view?usp=sharing` URL where the direct-download
form is needed.

**"templates changed since the last test send".**
You edited the copy. Re-run `outbound test-email --mailbox <id> --campaign <name>`
and look at the result. The gate exists so an edit cannot reach strangers
without you having seen it rendered once.

**Drafts do not appear in Gmail.**
Check `outbound drafts` first — if they are listed there, they were created. Look
in Gmail's Drafts folder, not the inbox. If IMAP failed you will see it in the
send output rather than silently.

**"command not found: outbound".**
Open a new terminal. If it persists, `outbound doctor` will tell you which shell
you are in and which profile files it checked.
