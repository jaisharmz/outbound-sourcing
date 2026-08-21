"""Choose which contacts get a fully rendered preview in the review export.

Taking the first five rows shows the happy path five times. A blank line in the
`personalization: null` branch shipped from milestone 2 to milestone 5 without
being noticed, and null is the *common* case -- two of the first three real
contacts had it. Previews that never render the common case cannot catch that.

So the five span the ways an email can differ, and each one says which axis it
was chosen for.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Ordered by how much each axis has actually cost. Null personalization first,
# because that is the branch a real bug shipped in.
AXES: list[tuple[str, str]] = [
    ("personalization IS NULL", "personalization is null, so the template falls back"),
    ("email_basis = 'inferred_from_pattern'", "address was inferred from a pattern, not observed"),
    ("personalization IS NOT NULL", "personalization is present and sourced"),
    ("email_basis = 'observed'", "address was observed in a document"),
    ("verification_status = 'catch_all'", "domain is accept-all, so verification could not confirm it"),
]


@dataclass
class Preview:
    contact_id: int
    reason: str


def choose(conn: sqlite3.Connection, *, campaign: str | None = None,
           limit: int = 5) -> list[Preview]:
    """Pick contacts spanning the axes, then fill from whatever is left."""
    where = ["approved = 0"]
    params: list = []
    if campaign:
        where.append("campaign = ?")
        params.append(campaign)
    base = " AND ".join(where)

    picked: list[Preview] = []
    seen: set[int] = set()

    for clause, reason in AXES:
        if len(picked) >= limit:
            break
        row = conn.execute(
            f"SELECT id FROM contacts WHERE {base} AND {clause}"
            + (f" AND id NOT IN ({','.join('?' * len(seen))})" if seen else "")
            + " ORDER BY id LIMIT 1",
            (*params, *seen),
        ).fetchone()
        if row:
            picked.append(Preview(row["id"], reason))
            seen.add(row["id"])

    if len(picked) < limit:
        rows = conn.execute(
            f"SELECT id FROM contacts WHERE {base}"
            + (f" AND id NOT IN ({','.join('?' * len(seen))})" if seen else "")
            + " ORDER BY id LIMIT ?",
            (*params, *seen, limit - len(picked)),
        ).fetchall()
        for r in rows:
            picked.append(Preview(r["id"], "filler, no unrepresented axis left"))
    return picked
