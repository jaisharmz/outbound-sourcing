"""Count what a run actually costs: HTTP calls, API calls, wall time.

The operator needs to know whether /outbound on one company is two minutes or
twenty before deciding to run it on fifty. That answer has to be measured, not
estimated, so the modules that make network calls increment counters here and
the run report sums them.

Counters are in-process and mirrored to a per-run JSON file, because a single
/outbound run is several CLI invocations in separate processes. The file is the
accumulator; the in-process dict is just what one process contributes.

Agent-side work -- WebSearch and WebFetch calls the model makes directly -- can
only be reported by the agent, via `outbound run log --searches N`. Those are
recorded in the same file and labelled as agent-reported, so the total is never
silently understating the parts nobody instrumented.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_COUNTS: dict[str, float] = {}
_T0 = time.monotonic()


def runs_dir() -> Path:
    d = Path(__file__).resolve().parent.parent / "state" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def current_run() -> str | None:
    return os.environ.get("OUTBOUND_RUN_ID")


def bump(metric: str, n: float = 1) -> None:
    _COUNTS[metric] = _COUNTS.get(metric, 0) + n


def snapshot() -> dict:
    return {**_COUNTS, "elapsed_s": round(time.monotonic() - _T0, 2)}


def flush(run_id: str | None = None, label: str = "") -> dict | None:
    """Append this process's counters to the run file. Safe to call repeatedly."""
    run_id = run_id or current_run()
    if not run_id or not _COUNTS:
        return None
    path = runs_dir() / f"{run_id}.json"
    data = json.loads(path.read_text()) if path.exists() else {
        "run_id": run_id, "started_at": time.time(), "steps": []}
    data["steps"].append({"label": label or "step", **snapshot()})
    path.write_text(json.dumps(data, indent=2))
    _COUNTS.clear()
    return data


def start(run_id: str, target: str, kind: str) -> Path:
    path = runs_dir() / f"{run_id}.json"
    path.write_text(json.dumps({
        "run_id": run_id, "target": target, "kind": kind,
        "started_at": time.time(), "steps": []}, indent=2))
    return path


def report(run_id: str) -> dict:
    path = runs_dir() / f"{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no run file for {run_id!r}")
    data = json.loads(path.read_text())
    totals: dict[str, float] = {}
    for step in data["steps"]:
        for k, v in step.items():
            if k == "label":
                continue
            totals[k] = totals.get(k, 0) + v
    data["totals"] = totals
    data["wall_s"] = round(time.time() - data["started_at"], 1)
    return data
