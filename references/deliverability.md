# Deliverability

## Scope

One mailbox, 15–25 sends a day, invoked by hand. The pool, the warmup ramp, the dedicated
domains and the circuit breaker are gone — see the removal table in `setup.md`. What
follows is what still applies at this volume.

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

**Authentication cannot be measured by mailing yourself.** A message from an account to
itself through the same provider never crosses an authentication boundary, so no
`Authentication-Results`, `Received-SPF` or `DKIM-Signature` header is ever added. The
delivered copy looks fine and tells you nothing. Use `--to` with a receiver on a different
provider:

```
outbound test-email --mailbox <id> --to you@outlook.com                 # placement + verdicts
outbound test-email --mailbox <id> --to <one-time@mail-tester.com>      # full report
```

Two things to check the first time this is configured, both visible in the raw headers at
the *receiving* end:

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

## No tracking pixels

Deliverability negative, researchers notice and it costs you credibility with exactly the
audience you want, and reply rate is the metric that matters anyway.

## Attachments and links

Base64 inflates an attachment by 4/3, and a receiving gateway measures the encoded size.
Many corporate gateways reject inbound above 10 MB and some above 5 MB, so an oversized set
**hard-bounces for reasons that have nothing to do with address quality**. That is true at
20 sends a day exactly as it is at 500, which is why `max_attachment_bytes` and the
campaign gate both stay.

Attachments and links are independent, not an either/or: a first touch attaches what fits
and links what does not. First touch is a resume plus a technical document at roughly 1 MB
on the wire, with the 6.2 MB portfolio linked.

## Removed: the circuit breaker

The trailing-bounce halt existed to stop a runaway campaign from burning a dedicated
sending domain. There is no dedicated domain now, and at 20 sends a day a bad list is
visible by eye before it is visible in a rate.

Bounce detection itself stayed. It feeds the permanent suppression list, and address
quality is independent of volume.
