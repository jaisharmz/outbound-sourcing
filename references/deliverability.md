# Deliverability

## Volume ceilings

500/day is not a single-inbox number. Realistic cold-outbound ceilings are **30–50
sends/day/mailbox** before filters start scoring you down, whatever the provider's
nominal cap says.

| Target | Mailboxes |
|---|---|
| 100/day | ~3 |
| 300/day | ~8 |
| 500/day | 12–15 |

The pool exists from day one even when it holds one entry. Retrofitting multi-mailbox
onto a single-mailbox sender is painful, and the cap accounting, round-robin, and warmup
ramp all change shape when there is more than one.

## CC accounting — what is actually binding

Both of these are true and they point in different directions:

**CC'd addresses count against the provider's daily *recipient* cap, not the message
cap.** So the scheduler counts recipients per message, not messages. `mailbox_day` tracks
both, and `RenderedEmail.recipient_count` is `1 + len(cc) + len(bcc)`.

**But that is rarely the binding constraint.** Google Workspace allows on the order of
2,000 recipients/day. The real limit is the 30–50/day/mailbox reputation ceiling, and
reputation is driven by messages sent to strangers — not by a copy sent to an address you
own. If the default CC is the operator's own group inbox, it consumes recipient-cap
headroom that was never going to bind and costs approximately nothing in reputation.

So: **a self-CC does not halve capacity.** Four mailboxes at 40/day is ~160 contacts/day,
not ~80. Recipient counting stays because it is correct for the provider cap and because
a CC list that grows to real third parties would start to matter.

The other thing worth knowing: the CC is visible to the recipient. That is arguably good.
It reads like a real organization rather than one person mail-merging.

## The From / Reply-To split

Cold sends go `From:` the dedicated sending domain with `Reply-To:` the address the
sender actually reads. This keeps a primary or institutional address out of the
reputation blast radius while still being the address people answer.

Two things to check the first time this is configured, both visible in the raw headers
that `test-email` prints:

- **SPF/DKIM/DMARC align against the `From:` domain**, not the Reply-To. A misaligned
  Reply-To is normal and harmless; a misaligned From is the whole problem.
- **A From/Reply-To mismatch is a mild spam heuristic** in some filters. It is very
  widely used and generally fine, but confirm placement with a real test send rather than
  assuming.

Institutional Workspace tenants frequently block third-party OAuth clients from holding
`gmail.send` on their accounts. Find that out early — it is an admin policy, not
something code can work around.

## Domains

Do not run this volume through a primary personal or institutional address. Dedicated
sending domains with SPF, DKIM and DMARC; the primary address for replies only. Setup
steps are in `setup.md`.

## Warmup

Default ramp: 10/day, +5/day, until the mailbox's `daily_cap`. From `warmup_start_date`
per mailbox, so mailboxes added later ramp on their own clock rather than inheriting the
pool's age.

A domain needs age as well as volume ramp. Two to three weeks between first send and
meaningful volume is normal and cannot be compressed by sending more.

## No tracking pixels

Deliverability negative, researchers notice and it costs you credibility with exactly the
audience you want, and reply rate is the metric that matters anyway.

## Attachments vs links on first touch

Three PDFs on a first-touch cold email is a strong spam signal and a real part of why
cold campaigns land in Promotions. Total payload matters as much as count — a 14 MB
first-touch message from an unknown sender is close to the worst available signal.

The system supports both and settles the question with data:

- `campaign.yaml: step1_variant: attachments | links`
- links resolve against `links_base_url`, which should sit on the **sending domain** —
  link-domain alignment with the From domain is worth more than convenience, and a link
  to a third-party file host is its own filter signal
- the variant is stamped on every `messages` row, so reply and bounce rate break down per
  variant
- later steps always attach; the A/B is about first touch only, since by step two the
  recipient has already seen you

Run it as a real split, not a switch flipped once, and give it enough volume to say
something.

## The circuit breaker

If the trailing-200-send bounce rate exceeds the threshold (default 2%), all sending
halts and requires a manual `--resume`. This is the single most important safety
mechanism in the system. A runaway bad-address campaign burns a sending domain
permanently, and that is not recoverable — no amount of later good behaviour buys the
reputation back.

Do not raise the threshold to get unblocked. Find the bad addresses.
