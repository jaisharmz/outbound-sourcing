"""Nothing ships that nothing calls.

Three things looked implemented, passed their own tests, and were never invoked
by anything: review.py had no CLI command, suppress_lab wrote rows is_suppressed
never read, and leadership.py only ever ran from an ad-hoc script. Each was
found by accident. A fourth -- apply_pattern -- was found by this file.

The failure is specific and quiet: a module with tests looks like working code,
and an uncalled check is indistinguishable from a check that passes. So
reachability is asserted rather than watched.
"""

from __future__ import annotations

import ast
import pathlib
import re

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
TESTS = pathlib.Path(__file__).resolve().parent
ENTRYPOINTS = {"outbound"}          # the console script; see pyproject [project.scripts]


def _modules() -> dict[str, pathlib.Path]:
    return {p.stem: p for p in SCRIPTS.glob("*.py") if p.stem != "__init__"}


def _sibling_imports(path: pathlib.Path, known: set[str]) -> set[str]:
    """Sibling modules imported anywhere, including inside function bodies."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module in known:
                out.add(node.module)
            if node.level and node.module is None:
                out |= {a.name for a in node.names if a.name in known}
            if node.module and node.module.startswith("scripts."):
                tail = node.module.split(".", 1)[1]
                if tail in known:
                    out.add(tail)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("scripts."):
                    tail = a.name.split(".", 1)[1]
                    if tail in known:
                        out.add(tail)
    return out


def test_every_module_is_reachable_from_an_entrypoint():
    """A module nobody imports is code that cannot run, however green its tests."""
    mods = _modules()
    graph = {m: _sibling_imports(p, set(mods)) for m, p in mods.items()}

    reached, stack = set(ENTRYPOINTS), list(ENTRYPOINTS)
    while stack:
        for dep in graph.get(stack.pop(), ()):
            if dep not in reached:
                reached.add(dep)
                stack.append(dep)

    unreachable = sorted(set(mods) - reached)
    assert not unreachable, (
        f"unreachable from `outbound`: {unreachable}. Wire it into a command or the "
        f"investigation loop, or delete it with its tests."
    )


# Functions registered by a decorator or a lookup table are called by name, not
# by a call expression, so a caller search cannot see them. Listed explicitly so
# the exemption is visible and has to be justified rather than inferred.
REGISTERED = {
    # investigate.STEPS dispatches these by lead kind.
    "step_person", "step_homepage", "step_scholar", "step_paper",
    "step_domain_pattern", "step_title_hunt", "step_enrichment",
}


def _is_typer_command(node: ast.FunctionDef) -> bool:
    for dec in node.decorator_list:
        src = ast.dump(dec)
        if "command" in src or "callback" in src:
            return True
    return False


def test_no_public_function_is_dead():
    """Catches the suppress_lab shape: a function inside a live module that
    nothing in production ever references, kept alive only by its own test.

    Counts references rather than calls, because a function can be reached by
    being named -- in a dispatch table, or passed as an argument -- and demanding
    a call expression reports those as dead.
    """
    mods = _modules()
    body = "\n".join(p.read_text() for p in mods.values())
    dead = []
    for mod, path in sorted(mods.items()):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name.startswith("_") or name == "main" or name in REGISTERED:
                continue
            if _is_typer_command(node):
                continue
            # Any reference counts, not just a call expression. A function
            # named as a dispatch-table value -- {"namesake": skip_namesake} --
            # is reachable, and requiring `name(` reported those as dead. A
            # genuinely dead function appears exactly once: in its own def.
            # A preceding dot is allowed: meter.bump and G.upsert_node are
            # calls. Excluding them reported half the codebase as dead.
            refs = len(re.findall(rf"(?<![\w]){re.escape(name)}\b", body))
            defs = len(re.findall(rf"def {re.escape(name)}\b", body))
            if refs <= defs:
                dead.append(f"{mod}.{name}")
    assert not dead, (
        f"public functions with no caller in scripts/: {dead}. A test is not a "
        f"caller -- wire it in or delete it."
    )


# ------------------------------------------------- failures must be loud


def test_a_failed_mx_lookup_is_unknown_not_invalid(monkeypatch):
    """Returning [] for both "this domain accepts no mail" and "the lookup did
    not complete" made a DNS timeout indistinguishable from a dead domain, and
    probe marked the address invalid on the strength of it -- discarding a real
    contact because the network hiccuped."""
    from scripts import verify

    monkeypatch.setattr(verify, "mx_lookup",
                        lambda d, timeout=8: ([], "MX lookup for x did not complete (Timeout)"))
    status, detail = verify.probe("someone@example.test")
    assert status == verify.UNKNOWN
    assert "did not complete" in detail

    monkeypatch.setattr(verify, "mx_lookup", lambda d, timeout=8: ([], None))
    status, detail = verify.probe("someone@example.test")
    assert status == verify.INVALID
    assert "no MX record" in detail


def test_a_failed_org_lookup_is_not_a_verdict(monkeypatch):
    """An empty roster from a failed request reads as "nobody works there",
    which is a different claim from "the request did not complete"."""
    from scripts import hf_org

    hf_org._LAST_FAILURE.clear()
    monkeypatch.setattr(hf_org, "members", lambda slug: [])
    ok, why = hf_org.check("Hugging Face", "Someone")
    assert ok is None and "private, renamed or empty" in why

    hf_org._LAST_FAILURE["huggingface"] = "HTTP 503"
    ok, why = hf_org.check("Hugging Face", "Someone")
    assert ok is None and "lookup failed (HTTP 503)" in why
