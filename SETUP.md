# Setup

Twenty minutes. Six steps. Each one says what to do, how you know it worked, and
what usually goes wrong.

If something breaks, run `outbound doctor` — it names the problem and the fix.
That is the only debugging command you need.

---

## Read this first: what this tool is for

It finds people at a company, proves who they are, and leaves personalized
drafts in your Gmail. **You** open Gmail and send them by hand. Nothing sends
itself.

**It works on some populations and not others.** This is the single most
important thing to know before you point it at anything, because pointing it at
the wrong kind of target returns nothing and looks broken.

| you are targeting | does it work | why |
| --- | --- | --- |
| **AI/ML researchers who publish** (Together AI, Mistral, Hugging Face) | **yes, well** | they keep personal sites and publish papers with contact details |
| **Systems / hardware engineers** (Groq, Cerebras, d-Matrix) | **yes, differently** | few personal pages; addresses come from paper first pages and commit patterns |
| **Product companies that don't publish** (Baseten, Fireworks) | **yes** | no papers, but public GitHub orgs give the email convention |
| **Open-source projects** (LangChain, vLLM, LlamaIndex, Unsloth) | **no — expect zero** | contributors commit from personal addresses, so there is no company convention to learn and no company address to find |

A run that returns nothing for an open-source project is the tool working
correctly. It is not broken and you have not misconfigured it.

Start with a company that publishes research. You will see it work, and then you
will know what a real zero looks like.

---

## 1. Install

```bash
git clone <repo-url> outbound-sourcing
cd outbound-sourcing
./install.sh
```

The installer creates a Python environment, installs dependencies, links the
skill into `~/.claude/`, and finishes by running `outbound doctor`.

**Worked if:** you see a list of checks and `Ready.` at the end. Some will fail
— you have not added credentials yet. That is expected.

**Usually goes wrong:** `python3: command not found`. Install Python 3.11+
(`brew install python@3.13` on macOS) and run `./install.sh` again. It is safe
to re-run any number of times, and it never overwrites your config or contacts.

**If `outbound` is not found afterwards:** open a *new* terminal. The installer
adds a line to your shell profile and your current shell has not read it yet.

---

## 2. Gmail app password

**Not OAuth. Not a Google Cloud project.** This tool sends over SMTP and writes
drafts over IMAP, both with a 16-character app password. There is no API key and
nothing to register.

1. Turn on 2-Step Verification: **myaccount.google.com/signinoptions/two-step-verification**
   App passwords do not exist without it — this is the step people skip.
2. Go to **myaccount.google.com/apppasswords**
3. Name it `outbound`, create it, copy the 16 characters.
4. Put it in `config/secrets.env`:
   ```
   GMAIL_APP_PASSWORD_PERSONAL=abcd efgh ijkl mnop
   ```
   Spaces are fine. Keep the quotes off.

**Worked if:** `outbound doctor` shows `ok  mailbox authenticates`.

**Usually goes wrong:** `535 Username and Password not accepted`. Either 2FA is
off, or you pasted your normal Google password. App passwords are 16 characters
and only appear once — if you lost it, delete and make a new one.

> **If your account is a university or company Workspace account** (`@berkeley.edu`,
> `@company.com`), your admin may block app passwords entirely. Use a personal
> Gmail as the sender and put your institutional address in `config/cc.yaml` and
> as `reply_to` in `config/mailboxes.yaml`. Replies will reach the right inbox.

---

## 3. GitHub token

Used to learn a company's email convention from its public commits. This is the
main address source for companies that don't publish papers.

1. Go to **github.com/settings/personal-access-tokens/new**
2. Give it a name and an expiry.
3. **Select no scopes at all.** Public read is the default and is everything this
   needs. A token with no permissions can do no damage if it leaks.
4. Put it in `config/secrets.env`:
   ```
   GITHUB_TOKEN=github_pat_...
   ```

**Worked if:** `outbound doctor` shows `ok  github api reachable` with a request
count.

**Usually goes wrong:** skipping this. Without a token you get 60 requests an
hour and most runs will find nothing and report an honest zero. It is a warning
rather than an error, so it is easy to miss.

---

## 4. Your persona

Edit `config/persona.md`. This is you, and it appears in every email.

```yaml
name: Ada Lovelace
first_name: Ada
role: lead an industry research group     # completes "I ___ at ___"
org: Your University
links:
  Google Scholar profile: https://scholar.google.com/citations?user=...
  LinkedIn profile: https://www.linkedin.com/in/...
  GitHub profile: https://github.com/...
projects:
  - org: Some Lab
    blurb: what you built there, in one clause
```

The `projects` list becomes the bullets in the email. Three or four, each one
clause, concrete.

**Worked if:** `outbound doctor` shows `ok  config loads`.

**Usually goes wrong:** leaving a `[PLACEHOLDER]` in. Campaigns refuse to start
while one is present — deliberately, because the alternative is an email that
goes out saying `[NAME]`.

---

## 5. Attachments and links

Put your PDFs in the directory named by `attachments_root` in
`config/campaign.yaml`, then list them in `config/sequence.yaml`.

```yaml
documents:
  - name: Resume
    file: Your_Resume.pdf
    url: https://drive.google.com/uc?export=download&id=FILE_ID
```

**The size rule:** anything that fits under the cap is attached, anything that
does not is linked. You do not choose — it is computed from real file sizes.
Give every document a `url` so the big ones have somewhere to go.

**Drive links must be the direct-download form.** From a share link
`https://drive.google.com/file/d/FILE_ID/view?usp=sharing`, take `FILE_ID` and
build `https://drive.google.com/uc?export=download&id=FILE_ID`. Config rejects
the `/view` form and prints the corrected URL.

**Set sharing to "Anyone with the link".** In Drive: Share → General access →
Anyone with the link. A link that asks the recipient to request access is worse
than no link — they click, get a permission wall, and the email reads as
careless.

**Worked if:** `outbound doctor` shows `ok  linked documents open` and
`ok  attachments fit`.

**Usually goes wrong:** leaving sharing as "Restricted". Doctor catches it, but
only if you run it.

---

## 6. Your email template

`config/templates/startup/step1_initial.md` is the worked example. It is HTML;
bold renders as bold and links render as clickable text.

Two rules:

- **Nothing is appended.** What the template says is what sends — no footer, no
  signature block added for you. If you want something at the bottom, put it in
  the template.
- **`{{ personalization }}` may be empty.** When the research finds nothing
  specific to say, that block is skipped rather than filled with a generic
  compliment. The template must read correctly either way.

Then send yourself one:

```bash
outbound test-email --mailbox gmail-smtp --campaign startup
```

**Worked if:** it arrives in your inbox and looks right. Open it. Check the
bold, the links, the attachments, and that your name is spelled correctly.

**Usually goes wrong:** editing the copy later and forgetting to re-run this.
The send gate compares a hash of your templates against the last test send and
refuses to start if they differ, so you will be told.

---

## Done

```bash
outbound doctor
```

Ten green checks means you are ready. Then read `USAGE.md` — the common path is
three commands.

Your first run should be a company that publishes research. Try:

```bash
outbound investigate "Together AI" --domain together.ai
```
