# outbound-sourcing

Finds named people at a company, proves who they are, and leaves personalized
emails as **drafts in your Gmail**. You read them and send by hand.

It never sends anything itself.

```bash
git clone <this-repo> outbound-sourcing
cd outbound-sourcing && ./install.sh
```

Then, in Claude Code:

```
/outbound Baseten
```

It searches, follows leads, resolves addresses, filters, and writes a draft per
person. Open Gmail, read them, send the ones you want — it notices what went out
on the next run.

---

## What makes it different from a scraper

**Every claim has a source you can open.** A contact record carries the URL and
the quoted text behind the person's name, their role, and their address. The
validator rejects a record whose evidence chain is incomplete, so a fabricated
contact cannot reach the queue.

**An address is either observed or inferred, and they are different claims.**
Observed means seen on a page, in a paper, or in a commit. Inferred means a
domain convention predicts it — applied only above 90% confidence, marked, and
flagged at review as `NOT OBSERVED`, because that is the one that bounces.

**A title is never inferred from activity.** Commit history says what someone
does, not what their role is. An unfound title stays `Unknown` and you decide.

**Discovery is an investigation loop, not a fixed pipeline.** A page with no
address but a Scholar link is a lead. A paper carrying company addresses makes
every coauthor a lead. It stops on budget or when consecutive steps stop
yielding, and logs every step so the reasoning is reviewable.

---

## It works on some populations and not others

This decides whether it is useful to you at all.

| targeting | works | why |
| --- | --- | --- |
| ML researchers who publish | **yes, well** | personal sites, papers with contact details |
| Systems / hardware engineers | **yes, differently** | paper first pages and commit patterns |
| Product companies that don't publish | **yes** | public GitHub orgs give the email convention |
| Open-source projects | **no — expect zero** | contributors commit from personal addresses, so there is no company convention to learn |

A run that returns nothing for an open-source project is the tool working
correctly.

---

## Gates between a record and a send

Each exists because something failed silently once.

evidence validator · suppression · personal exclusions · founders/leadership
filter · human review gate · link reachability · attachment size · template hash
· delivered-From check · production-database guard

---

## Docs

- **[SETUP.md](SETUP.md)** — six steps, ~20 minutes. No Google Cloud project, no
  OAuth: an app password over SMTP and IMAP.
- **[USAGE.md](USAGE.md)** — the common path is three commands, plus
  troubleshooting.
- **[SKILL.md](SKILL.md)** — what the agent follows.
- **[references/](references/)** — the evidence standard, deliverability, and a
  write-up of records that were confidently wrong and how each was caught.

`outbound doctor` diagnoses most problems and prints the fix for each.

---

## Running it as a group

Point everyone at one shared append-only CSV and collisions sort themselves out.
Sending records the claim, so nobody has to remember a new habit. See
[USAGE.md](USAGE.md#running-this-as-a-group).

## Privacy

`config/` and `state/` are gitignored and have never been tracked: your persona,
credentials, contacts, drafts and suppression list stay on your machine.
Contacts gathered for your outreach are yours — this repo ships none.
