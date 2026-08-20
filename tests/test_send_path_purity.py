"""The send path contains zero model calls, and that is checked, not promised.

Rule 1 of the spec: once a contact is approved and queued, sending is pure
mechanism. This test walks the transitive imports of every module the sender
touches and fails if any of them can reach a model, an agent framework, or an
LLM provider SDK. It is a structural guarantee -- if someone later imports an
SDK into the scheduler to "just classify this one edge case", the suite fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Modules the send path may never reach, directly or transitively.
FORBIDDEN_PREFIXES = (
    "anthropic", "openai", "google.generativeai", "google.genai", "cohere",
    "mistralai", "ollama", "litellm", "langchain", "llama_index", "transformers",
    "claude", "subprocess",
)

# Entry points of the deterministic send path. Everything reachable from these
# is in scope. As later milestones land, add their modules here.
SEND_PATH_ENTRIES = ["providers", "templates", "cc", "suppression", "normalize", "db", "config"]


def module_file(name: str) -> Path | None:
    rel = name.replace(".", "/")
    for candidate in (SCRIPTS / f"{rel}.py", SCRIPTS / rel / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the package
                out.add(node.module or "")
            elif node.module:
                out.add(node.module)
    return out


def reachable(entries: list[str]) -> dict[str, set[str]]:
    """Map every local module in the send path to what it imports."""
    seen: dict[str, set[str]] = {}
    queue = list(entries)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        path = module_file(name)
        if path is None:
            continue
        # A package: include its submodules too.
        if path.name == "__init__.py":
            for sub in path.parent.glob("*.py"):
                if sub.name != "__init__.py":
                    queue.append(f"{name}.{sub.stem}")
        seen[name] = imports_of(path)
        for imported in seen[name]:
            local = imported.split(".")[0]
            if module_file(imported):
                queue.append(imported)
            elif module_file(local):
                queue.append(local)
    return seen


def test_send_path_cannot_reach_a_model():
    graph = reachable(SEND_PATH_ENTRIES)
    assert graph, "send path entry modules not found"
    violations = []
    for module, imports in graph.items():
        for imported in imports:
            if any(imported == p or imported.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                violations.append(f"{module} imports {imported}")
    assert not violations, (
        "the send path must contain zero model calls; found:\n  " + "\n  ".join(violations)
    )


def test_providers_package_is_covered():
    graph = reachable(["providers"])
    assert "providers.console" in graph


@pytest.mark.parametrize("name", SEND_PATH_ENTRIES)
def test_every_declared_entry_exists(name):
    assert module_file(name) is not None, f"send path entry {name!r} has no module"
