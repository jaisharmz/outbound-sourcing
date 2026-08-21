# Opt-out, and why there is no footer

## The decision

Rendered emails carry **no appended footer**: no unsubscribe line, no physical
mailing address, no auto-generated anything. What the template says is what
sends, byte for byte. `tests/test_templates.py::test_render_is_byte_for_byte_the_template`
renders the template through Jinja directly and asserts the send path produced
exactly that, so the rule cannot erode by accident.

This is a deliberate choice, not a missing field. An earlier version of this
skill appended a CAN-SPAM footer to every message and blocked a campaign start
until a real street address was configured. Both were removed.

## The reasoning

CAN-SPAM's footer requirements attach to *commercial electronic mail* — messages
whose primary purpose is advertising or promoting a commercial product or
service. What this system sends is a personally reviewed email proposing a
research collaboration, at 15–25 a day, from a named individual at a named
university, to a specific person chosen because of specific published work that
is quoted in the message. No product is offered and nothing is for sale.

Beyond the legal question, the footer actively worked against the goal. A `-- `
separator followed by an unsubscribe line and a block address is the visual
signature of a mail merge. The recipients here are researchers who see a great
deal of automated outreach and have learned to recognize it in the first second.
A message that reads as personally written and is personally written should not
carry the costume of bulk mail.

## What the obligation actually is

**Honoring an opt-out, not advertising one.** That part is kept in full and is
the part that matters ethically and for deliverability:

- `outbound suppress <email>` — one line, no subcommand, kind inferred from
  shape. The moment someone asks to stop, this is the whole command.
- Bounces feed permanent suppression automatically.
- Detected replies stop the sequence, and a reply asking to stop suppresses at
  the company level.
- Suppression is permanent, global, checked on every send, and mirrored to
  `config/suppression.csv` so it survives the database.

A recipient who replies "please stop" gets the same outcome they would get from
clicking an unsubscribe link, and gets it from a human who read their reply.

## Revisit this if

- **Volume rises substantially.** The reasoning rests on low volume and
  individual review. Sending materially more per day, or sending without reading
  each one, changes the character of the mail and this should be reconsidered.
- **The copy shifts toward selling.** If a template starts promoting a service,
  a product, or paid work rather than proposing collaboration, it is commercial
  mail and the footer requirements apply — reinstate them before that ships.
- **Sending moves to a dedicated domain or an ESP.** Most ESPs require an
  unsubscribe header or footer contractually, independent of the law.

A cheap middle path if any of the above happens: add `List-Unsubscribe` and
`List-Unsubscribe-Post` headers. They give mail clients a native one-click
opt-out without putting bulk-mail furniture in the visible body.
