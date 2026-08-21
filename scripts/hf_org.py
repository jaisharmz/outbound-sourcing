"""Current employees from a company's Hugging Face organisation.

Solves two problems the other channels could not.

**Staleness.** An OpenAlex affiliation string records where someone worked when
a paper was submitted. Cross-checking fifteen Hugging Face addresses against the
live org list, nine were people who had left -- Douwe Kiela to Google DeepMind,
Nazneen Rajani to her own company. Sixty percent stale, and every one of those
records looked perfect. Org membership is maintained by the company and reflects
now, so it is a verification oracle the paper and page channels lack.

**Companies that do not publish.** OpenAlex found one person at Baseten and
three at Fireworks AI, because neither company publishes research. Their HF orgs
list 121 and 70 current members. The people were always there; the academic
index was simply the wrong place to look for them.

The caveat, stated because it bounds every claim built on this: org membership
is not a contract of employment. Companies sometimes admit collaborators or
contractors, and a departed employee is usually but not always removed. It is
strong evidence of a current association, which is why it is used to *filter*
rather than to assert a title -- the title still has to come from somewhere the
person says it themselves.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import meter

API = "https://huggingface.co/api"

# Below this many members, a *negative* result means nothing. Mistral AI's org
# lists 13 people against a company of hundreds, so "not on the roster" there is
# a statement about the org's coverage, not about the person -- it marked all
# five Mistral candidates as departed. Positives stay trustworthy at any size:
# being listed is evidence regardless of how many others are.
MIN_ROSTER_FOR_NEGATIVES = 50
UA = {"User-Agent": "outbound-sourcing/1.0 (mailto:jaisharmaus@gmail.com)"}

# Company -> HF org slug. The slug is rarely the company name.
ORG_SLUGS = {
    "together ai": "togethercomputer", "groq": "Groq", "baseten": "baseten",
    "fireworks ai": "fireworks-ai", "sambanova": "sambanovasystems",
    "cerebras": "cerebras", "neural magic": "neuralmagic",
    "perplexity ai": "perplexity-ai", "mistral ai": "mistralai",
    "hugging face": "huggingface", "d-matrix": "d-matrix", "modular": "modularai",
}


def name_key(name: str) -> str:
    """Sorted tokens, so reversed name order still matches one person."""
    return " ".join(sorted(t for t in re.split(r"[^a-z]+", (name or "").lower()) if t))


def slug_for(company: str) -> str | None:
    return ORG_SLUGS.get(company.strip().lower())


def members(slug: str) -> list[dict]:
    """Current members of one org. Empty list if the org is private or absent."""
    meter.bump("hf_calls")
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(f"{API}/organizations/{slug}/members", headers=UA),
            timeout=30).read()
    except urllib.error.HTTPError:
        return []
    except Exception:
        return []
    out = []
    for m in json.loads(raw):
        full = (m.get("fullname") or "").strip()
        if not full or m.get("type") != "user":
            continue
        out.append({"name": full, "user": m.get("user"), "key": name_key(full)})
    return out


def current_at(company: str) -> dict[str, dict]:
    """name_key -> member, for the company's org. Empty when there is no org."""
    slug = slug_for(company)
    return {m["key"]: m for m in members(slug)} if slug else {}


def check(company: str, name: str) -> tuple[bool | None, str]:
    """(is_current, why). None means the question could not be asked."""
    slug = slug_for(company)
    if not slug:
        return None, f"no Hugging Face org known for {company!r}"
    roster = current_at(company)
    if not roster:
        return None, f"org {slug!r} returned no members (private, renamed or empty)"
    hit = roster.get(name_key(name))
    if hit:
        return True, (f"listed as a current member of the {slug!r} Hugging Face org "
                      f"as @{hit['user']}")
    if len(roster) < MIN_ROSTER_FOR_NEGATIVES:
        return None, (f"the {slug!r} org lists only {len(roster)} members, too few to "
                      f"cover the company, so absence from it says nothing about this "
                      f"person")
    return False, (f"not in the {slug!r} Hugging Face org member list ({len(roster)} "
                   f"members), so the affiliation is likely out of date")


# Extended roster, added as the loop was pointed at more companies.
ORG_SLUGS.update({
    "modal labs": "modal-labs", "replicate": "replicate",
    "predibase": "predibase", "anyscale": "anyscale",
    "langchain": "langchain-ai", "weights & biases": "wandb",
    "chroma": "chroma-core", "llamaindex": "llamaindex",
    "unsloth": "unsloth", "vllm project": "vllm-project",
})
