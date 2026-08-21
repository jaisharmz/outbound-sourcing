"""The questions the loop stops to ask, and the ones it must not.

The tool runs unattended by default: search, follow leads, resolve addresses,
filter, render, draft. It does not narrate mechanics and does not ask permission
for anything already implied by what the operator asked for.

It interrupts for four shapes only, each a judgment the operator would want to
make and the tool cannot make honestly:

    expand_company   a promising company surfaced while researching another
    expand_industry  an adjacent subfield looks relevant
    seniority        someone senior enough that a cold email may be wrong
    ambiguous        something that would otherwise become a fabricated claim

Question design is the load-bearing part. A question that takes a paragraph to
read costs more attention than the decision is worth, and one asked twelve times
teaches the operator to stop reading. So: one line of context, one question, two
or three one-word answers, and `always`/`never` to settle the class for the rest
of the run.

Autonomy comes from config: `ask` (default), `auto` (expand freely, report at
the end), `strict` (never expand beyond what was named). Under every setting the
run summary lists each expansion decision, so nothing happens invisibly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ASK, AUTO, STRICT = "ask", "auto", "strict"

# What each shape means, and what a bare y/n does with it.
EXPAND_COMPANY = "expand_company"
EXPAND_INDUSTRY = "expand_industry"
SENIORITY = "seniority"
AMBIGUOUS = "ambiguous"

# Under `auto` these expand without asking; under `strict` they are refused.
EXPANSIONS = {EXPAND_COMPANY, EXPAND_INDUSTRY}


@dataclass
class Question:
    kind: str
    context: str          # one line, past tense, states what was found
    ask: str              # the question itself, ends in ?
    options: tuple[str, ...] = ("y", "n")

    def render(self) -> str:
        return f"{self.context}\n{self.ask}\n  {' / '.join(self.options)}"


@dataclass
class Decision:
    question: Question
    answer: str
    asked: bool           # False when a standing preference or autonomy decided it
    why: str = ""

    @property
    def yes(self) -> bool:
        return self.answer in ("y", "always")


@dataclass
class Session:
    """Tracks standing answers so the same question is not asked twelve times."""
    autonomy: str = ASK
    standing: dict[str, str] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)

    def resolve(self, q: Question, answer_fn=None) -> Decision:
        if q.kind in self.standing:
            d = Decision(q, self.standing[q.kind], asked=False,
                         why=f"you answered '{self.standing[q.kind]}' to this "
                             f"earlier in the run")
        elif self.autonomy == AUTO and q.kind in EXPANSIONS:
            d = Decision(q, "y", asked=False, why="autonomy: auto")
        elif self.autonomy == STRICT and q.kind in EXPANSIONS:
            d = Decision(q, "n", asked=False, why="autonomy: strict")
        else:
            answer = (answer_fn or _default_answer)(q)
            if answer in ("always", "never"):
                self.standing[q.kind] = answer
            d = Decision(q, answer, asked=True)
        self.decisions.append(d)
        return d

    def summary(self) -> list[str]:
        """Every expansion decision, however it was made. Nothing invisible."""
        out = []
        for d in self.decisions:
            mark = "+" if d.yes else "-"
            how = "asked" if d.asked else d.why
            out.append(f"  {mark} {d.question.context}  [{how}]")
        return out


def _default_answer(q: Question) -> str:
    raise RuntimeError("no answer function supplied")


# ------------------------------------------------------------- the questions


def company_found(found: int, company: str, while_researching: str) -> Question:
    return Question(
        EXPAND_COMPANY,
        f"Found {found} people at {company} while searching {while_researching}.",
        f"Include {company}?",
        ("y", "n", "always"))


def industry_adjacent(field_name: str, seen_in: str) -> Question:
    return Question(
        EXPAND_INDUSTRY,
        f"{seen_in} keeps turning up {field_name} work.",
        f"Explore {field_name} too?",
        ("y", "n", "always"))


def senior_person(name: str, title: str, company: str) -> Question:
    return Question(
        SENIORITY,
        f"{name} — {title} at {company}. Senior.",
        "Draft anyway?",
        ("y", "n", "never"))


def ambiguous_claim(name: str, what: str) -> Question:
    return Question(
        AMBIGUOUS,
        f"{name}: {what}",
        "Include the claim?",
        ("y", "n"))


# --------------------------------------------------------------- run summary


def run_summary(drafted: list[tuple[str, str]], skipped: list[tuple[str, str]],
                session: "Session | None" = None,
                read_first: list[str] | None = None) -> str:
    """One clear statement of what happened, in the operator's terms.

    Not a log. The operator wants to know how many messages are waiting, who
    they are to, what did not make it and why, and what to look at before
    sending. Everything else belongs in the investigation log.
    """
    from collections import Counter

    lines = []
    n = len(drafted)
    by_company = Counter(c for _, c in drafted)
    lines.append(f"{n} draft{'s' if n != 1 else ''} in your Gmail"
                 + (f", from {len(by_company)} compan"
                    f"{'ies' if len(by_company) != 1 else 'y'}:" if n else "."))
    for company, count in by_company.most_common():
        lines.append(f"    {count:>2}  {company}")

    if skipped:
        reasons = Counter(r for _, r in skipped)
        lines.append("")
        lines.append(f"{len(skipped)} skipped:")
        for reason, count in reasons.most_common():
            lines.append(f"    {count:>2}  {reason}")

    if session and session.decisions:
        lines.append("")
        lines.append("Expansion decisions:")
        lines.extend(session.summary())

    lines.append("")
    if read_first:
        lines.append("Read these before sending:")
        for item in read_first:
            lines.append(f"    - {item}")
    else:
        lines.append("Nothing flagged. Read a couple anyway before sending.")
    lines.append("")
    lines.append("Open Gmail, read them, send by hand. Then: outbound mark-sent --all")
    return "\n".join(lines)
