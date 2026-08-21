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

    def delegated_line(self) -> str:
        """One line for what a standing answer decided without being asked again.

        Two keystrokes can decide most of a run, and the operator should be able
        to see how much at a glance rather than counting bracketed notes.
        """
        from collections import Counter

        counts = Counter()
        for d in self.decisions:
            if d.asked or d.why.startswith("autonomy"):
                continue
            counts[(d.question.kind, "included" if d.yes else "skipped",
                    self.standing.get(d.question.kind, "?"))] += 1
        if not counts:
            return ""
        nouns = {EXPAND_COMPANY: ("company", "companies"),
                 EXPAND_INDUSTRY: ("field", "fields"),
                 SENIORITY: ("person", "people"),
                 AMBIGUOUS: ("claim", "claims")}
        parts = []
        for (kind, verb, ans), n in counts.most_common():
            one, many = nouns.get(kind, ("item", "items"))
            parts.append(f"{n} {one if n == 1 else many} {verb} by '{ans}'")
        return "standing answers decided: " + ", ".join(parts)

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


# ----------------------------------------------------------------- skips
#
# Why someone did not make it, and whether that is the end of it. The
# distinction decides what the operator does next: a final skip is a name to
# forget, a recoverable one is a name to chase by hand or re-run with more
# evidence. Collapsing them into one list makes every skip look like a dead end.

FINAL, RECOVERABLE = "final", "recoverable"


@dataclass
class Skip:
    name: str
    reason: str
    kind: str = FINAL
    unblocks: str = ""      # what would change the answer, when recoverable

    def line(self) -> str:
        if self.kind == RECOVERABLE and self.unblocks:
            return f"{self.reason} -- recoverable: {self.unblocks}"
        if self.kind == RECOVERABLE:
            return f"{self.reason} -- recoverable"
        return f"{self.reason} -- final"


# The skips the pipeline actually produces, with the judgement already made so
# callers do not each decide it differently.
def skip_leadership(name: str, where: str) -> Skip:
    return Skip(name, f"on the founders/leadership page ({where})", FINAL)


def skip_namesake(name: str, company: str) -> Skip:
    return Skip(name, f"page never mentions {company}", RECOVERABLE,
                f"find a page or paper that ties {name} to {company} and re-run")


def skip_no_address(name: str) -> Skip:
    return Skip(name, "no address found anywhere", RECOVERABLE,
                "a newer paper, a personal page, or a GitHub org would give one")


def skip_inferred(name: str) -> Skip:
    return Skip(name, "address inferred from the domain pattern, never observed",
                RECOVERABLE, "confirm it on a page or a paper first")


def skip_wrong_employer(name: str, saw: str) -> Skip:
    return Skip(name, saw, FINAL)


def skip_answered(name: str, answer: str) -> Skip:
    return Skip(name, f"you answered '{answer}' to this class", RECOVERABLE,
                "re-run and answer differently")


def skip_suppressed(name: str) -> Skip:
    return Skip(name, "suppressed: they asked not to be contacted", FINAL)


SKIP_BUILDERS = {
    "leadership": skip_leadership,
    "namesake": skip_namesake,
    "no-address": lambda name, ctx="": skip_no_address(name),
    "inferred": lambda name, ctx="": skip_inferred(name),
    "wrong-employer": skip_wrong_employer,
    "answered": skip_answered,
    "suppressed": lambda name, ctx="": skip_suppressed(name),
}


# --------------------------------------------------------------- run summary


def run_summary(drafted: list[tuple[str, str]],
                skipped: "list[Skip] | list[tuple[str, str]]",
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
        # Accept bare tuples so older callers keep working; they are treated as
        # final, which is the conservative reading.
        skips = [s if isinstance(s, Skip) else Skip(s[0], s[1], FINAL)
                 for s in skipped]
        lines.append("")
        n_rec = sum(1 for s in skips if s.kind == RECOVERABLE)
        head = f"{len(skips)} skipped"
        if n_rec:
            head += f" ({n_rec} recoverable, {len(skips) - n_rec} final)"
        lines.append(head + ":")
        for reason, count in Counter(s.line() for s in skips).most_common():
            lines.append(f"    {count:>2}  {reason}")

    if session and session.decisions:
        lines.append("")
        lines.append("Expansion decisions:")
        lines.extend(session.summary())
        # How much of the run was delegated with two keystrokes.
        delegated = session.delegated_line()
        if delegated:
            lines.append(f"  {delegated}")

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
