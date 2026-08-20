"""Config error reporting: turn Pydantic noise into something you can act on.

A typo in a YAML key must fail loudly at load time with a suggestion, never
silently at 500 sends/day. `humanize` resolves each error's location back to
the model class that rejected it, so the suggestion comes from that model's
real sibling fields rather than a global bag of names.
"""

from __future__ import annotations

import difflib
import typing
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ValidationError


class ConfigError(Exception):
    """Raised for any unusable configuration. Message is user-facing."""


def _unwrap(annotation: Any) -> list[type]:
    """Return the concrete classes an annotation could resolve to."""
    origin = get_origin(annotation)
    if origin is None:
        return [annotation] if isinstance(annotation, type) else []
    out: list[type] = []
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        out.extend(_unwrap(arg))
    return out


def _resolve_model(root: type[BaseModel], loc: tuple[Any, ...]) -> type[BaseModel] | None:
    """Walk a validation error location back to the model that owns it.

    `loc` for an extra-key error ends with the offending key, so the caller
    passes `loc[:-1]` to land on the containing model.
    """
    current: type[BaseModel] | None = root
    for part in loc:
        if current is None:
            return None
        if isinstance(part, int):
            continue  # list index: the annotation walk already stepped into the item type
        field = current.model_fields.get(str(part))
        if field is None:
            return None
        candidates = [c for c in _unwrap(field.annotation) if isinstance(c, type) and issubclass(c, BaseModel)]
        current = candidates[0] if candidates else None
    return current


def _suggest(unknown: str, known: typing.Iterable[str]) -> str | None:
    known = [k for k in known if k != unknown]
    matches = difflib.get_close_matches(unknown, known, n=1, cutoff=0.6)
    if matches:
        return matches[0]
    # difflib misses transpositions and joined words: `dailycap` vs `daily_cap`.
    squashed = unknown.replace("_", "").replace("-", "").lower()
    for k in known:
        if k.replace("_", "").lower() == squashed:
            return k
    return None


def _fmt_loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc) if loc else "(top level)"


def humanize(exc: ValidationError, model: type[BaseModel], source: Path | str) -> ConfigError:
    """Render a ValidationError as a ConfigError a human can fix in one pass."""
    lines = [f"{source}: {len(exc.errors())} configuration problem(s)."]
    for err in exc.errors():
        loc = tuple(err["loc"])
        if err["type"] == "extra_forbidden":
            key = str(loc[-1])
            owner = _resolve_model(model, loc[:-1])
            known = list(owner.model_fields) if owner else []
            where = _fmt_loc(loc[:-1])
            hint = _suggest(key, known)
            msg = f"  unknown key `{key}` under {where}"
            if hint:
                msg += f" — did you mean `{hint}`?"
            elif known:
                msg += f" — valid keys here: {', '.join(sorted(known))}"
            lines.append(msg)
        elif err["type"] == "missing":
            lines.append(f"  missing required key `{_fmt_loc(loc)}`")
        else:
            lines.append(f"  {_fmt_loc(loc)}: {err['msg']}")
    return ConfigError("\n".join(lines))
