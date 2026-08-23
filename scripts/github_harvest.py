"""Harvest observed email addresses from public GitHub commits.

Extracting an address from a commit is mechanism, not judgment, so it belongs in
a script. Its real value is not the addresses themselves but the pattern they
establish: one confirmed `first.last@` at a domain unlocks every name found
anywhere else.

Three things this is careful about, each because the naive version misleads:

**Throttling is not absence.** An unauthenticated probe returned "no repos" for
78 of 88 companies, which looked like a finding and was actually a rate limit.
Every outcome here is a named status, and `throttled` is never reported as
`no_public_repos`.

**A commit proves an address existed, not that the person is still there.** The
commit date is recorded, and anything older than the staleness window is pattern
evidence only -- never a sendable contact. Anyscale is the cautionary case: three
findable founders at a company acquired three weeks earlier.

**Bots and noreply addresses are not people.** They are filtered before anything
downstream can treat them as candidates.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
STALE_AFTER_DAYS = 730          # ~2 years

NOREPLY_MARKERS = (
    "users.noreply.github.com", "noreply", "no-reply", "donotreply",
)
BOT_MARKERS = (
    "dependabot", "renovate", "github-actions", "greenkeeper", "snyk-bot",
    "semantic-release", "codecov", "allcontributors", "[bot]", "svc-", "-bot@",
    "admin@", "ci@", "build@", "release@", "automation@", "auditor", "-runner",
    "deploy@", "noreply", "jenkins", "buildkite",
)

# Service and machine accounts that carry a person-shaped name. Real examples
# that slipped past the marker list: `flock-auditor`, `rasabot`, `rasa-aadlv`,
# `piotr-reducto`. The last shape matters most -- an org-suffixed handle is a
# login, not a name, and it cannot be matched to a title.
BOT_NAME_RE = re.compile(
    r"(^|[^a-z])bot([^a-z]|$)"          # 'bot' as its own word
    r"|^[a-z0-9_-]+-(bot|ci|admin|auditor)$"   # flock-auditor
    r"|^[a-z0-9_]*bot$",                # rasabot -- all-lowercase, bot suffix
    re.I,
)


def looks_like_handle(name: str, domain: str) -> bool:
    """True when the commit `name` is a login rather than a human name."""
    n = (name or "").strip()
    if not n:
        return True
    org = domain.split(".")[0].lower()
    low = n.lower()
    if org and (low.endswith("-" + org) or low.startswith(org + "-")):
        return True
    if BOT_NAME_RE.search(low):
        return True
    tokens = [t for t in re.split(r"[\s._-]+", n) if t]
    # A single token with no space is a handle unless it is a plain capitalised name.
    if len(tokens) < 2:
        return not (n[:1].isupper() and n.isalpha() and len(n) > 2)
    return False


def name_quality(name: str, domain: str) -> str:
    """`name` | `partial` | `handle`. Only a full name can be matched to a title."""
    if looks_like_handle(name, domain):
        return "handle"
    return "name" if name_is_usable(name, domain) else "partial"


def name_is_usable(name: str, domain: str) -> bool:
    """A name we could plausibly match to a title and an ICP role."""
    if looks_like_handle(name, domain):
        return False
    tokens = [t for t in re.split(r"[\s]+", name.strip()) if t]
    real = [t for t in tokens if t.isalpha() and len(t) > 1]
    return len(real) >= 2


def is_person_address(email: str, name: str = "") -> bool:
    e, n = email.lower(), (name or "").lower()
    if any(m in e for m in NOREPLY_MARKERS):
        return False
    if any(m in e or m in n for m in BOT_MARKERS):
        return False
    if BOT_NAME_RE.search(n):
        return False
    return "@" in e


# ------------------------------------------------------------------ transport


class RateLimiter:
    """Reads the budget off each response instead of guessing at it."""

    def __init__(self, floor: int = 25):
        self.remaining: int | None = None
        self.reset_at: float | None = None
        self.floor = floor
        self.throttled = False

    def note(self, headers) -> None:
        try:
            self.remaining = int(headers.get("X-RateLimit-Remaining", ""))
            self.reset_at = float(headers.get("X-RateLimit-Reset", ""))
        except (TypeError, ValueError):
            pass

    def should_pause(self) -> float:
        """Seconds to wait, or 0. Stops well before zero so a run degrades
        gracefully rather than turning into a wall of false negatives."""
        if self.remaining is None or self.remaining > self.floor:
            return 0.0
        if not self.reset_at:
            return 0.0
        return max(0.0, self.reset_at - time.time()) + 2


@dataclass
class Client:
    token: str | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)
    calls: int = 0

    def get(self, path: str) -> tuple[object | None, str]:
        """Returns (payload, status). Status is one of ok | throttled | missing | error."""
        wait = self.limiter.should_pause()
        if wait:
            if wait > 900:                       # do not sit for a quarter hour
                self.limiter.throttled = True
                return None, "throttled"
            time.sleep(wait)

        headers = {"User-Agent": "outbound-sourcing/0.1",
                   "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(path if path.startswith("http") else API + path,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                self.calls += 1
                self.limiter.note(resp.headers)
                return json.loads(resp.read()), "ok"
        except urllib.error.HTTPError as exc:
            self.limiter.note(exc.headers)
            if exc.code == 404:
                return None, "missing"
            if exc.code in (403, 429):
                self.limiter.throttled = True
                return None, "throttled"
            return None, "error"
        except Exception:
            return None, "error"


# ------------------------------------------------------------------ harvest


@dataclass
class DomainResult:
    company: str
    domain: str
    org: str | None = None
    status: str = "not_started"
    # email -> (name, most recent commit ISO date)
    addresses: dict[str, tuple[str, str]] = field(default_factory=dict)
    filtered: int = 0
    repos_seen: int = 0
    archived_repos: int = 0

    @property
    def newest_commit_at(self) -> str | None:
        dates = [when for _n, when in self.addresses.values() if when]
        return max(dates) if dates else None

    @property
    def fresh(self) -> dict[str, tuple[str, str]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)
        out = {}
        for em, (nm, when) in self.addresses.items():
            try:
                if datetime.fromisoformat(when.replace("Z", "+00:00")) >= cutoff:
                    out[em] = (nm, when)
            except ValueError:
                continue
        return out

    @property
    def stale(self) -> dict[str, tuple[str, str]]:
        fresh = self.fresh
        return {k: v for k, v in self.addresses.items() if k not in fresh}


# GitHub orgs whose login is neither the domain stem nor discoverable by search:
# the search path only accepts an org whose profile `blog` field contains the
# company domain, and most of these leave it unset or point it elsewhere. Without
# these, resolve_org returns no_org_found for companies that plainly have public
# code -- Toyota Research Institute (122 repos), foxglove (132), ros (84).
ORG_OVERRIDES = {
    '1x technologies': '1x-technologies',
    '8th wall': '8thwall',
    'abridge': 'abridgeai',
    'agility ai': 'agilityrobotics',
    'agility robotics': 'agilityrobotics',
    'agtonomy': 'agtonomy',
    'allen institute for ai': 'allenai',
    'ambi robotics': 'ambi-robotics',
    'ambience healthcare': 'Ambience-Healthcare',
    'anduril': 'anduril',
    'anduril defense': 'anduril',
    'anyscale': 'anyscale',
    'anyscale ray': 'anyscale',
    'apex space': 'apex-space',
    'applied intuition': 'appliedintuition',
    'arize ai': 'Arize-ai',
    'arize phoenix': 'Arize-ai',
    'assemblyai': 'AssemblyAI',
    'atom computing': 'atom-computing',
    'augment code': 'augmentcode',
    'aurora innovation': 'aurorainnovation',
    'ayar labs': 'AyarLabs',
    'bauplan': 'BauplanLabs',
    'bear robotics': 'bearrobotics',
    'bedrock robotics': 'Bedrock-Robotics',
    'berkshire grey': 'berkshiregrey',
    'beta technologies': 'Beta-Technologies',
    'bloomfield robotics': 'BloomfieldRobotics',
    'boardwalk robotics': 'boardwalkrobotics',
    'boston dynamics': 'boston-dynamics',
    'boston dynamics ai institute': 'bdaiinstitute',
    'brain corp': 'braincorp',
    'browserbase': 'browserbase',
    'built robotics': 'builtrobotics',
    'burro': 'burro-robotics',
    'camus energy': 'CamusEnergy',
    'capella space': 'capellaspace',
    'carbon robotics': 'carbonrobotics',
    'cartesia': 'cartesia-ai',
    'cerebras': 'Cerebras',
    'chalk ai': 'chalk-ai',
    'chaos group': 'ChaosGroup',
    'chef robotics': 'chef-robotics',
    'citrine informatics': 'CitrineInformatics',
    'civitai': 'civitai',
    'cleanlab': 'cleanlab',
    'cloudflare workers ai': 'cloudflare',
    'coactive ai': 'CoactiveAI',
    'coco robotics': 'cocorobotics',
    'coda': 'coda',
    'cognition': 'CognitionAI',
    'cohere': 'cohere-ai',
    'collaborative robotics': 'cobot',
    'comet ml': 'comet-ml',
    'comfy org': 'Comfy-Org',
    'commure': 'commure',
    'contextual ai': 'ContextualAI',
    'contextual vision': 'ContextualAI',
    'creatify': 'creatify-ai',
    'cresta': 'cresta',
    'cursor research': 'cursor',
    'cyngn': 'cyngn',
    'd-matrix': 'd-matrix-ai',
    'daily co': 'daily-co',
    'datature': 'datature',
    'daytona': 'daytona',
    'deep forest sciences': 'deepforestsci',
    'deep infra': 'deepinfra',
    'deepgram': 'deepgram',
    'deepnote': 'deepnote',
    'deforum': 'deforum',
    'determined ai': 'determined-ai',
    'diligent robotics': 'DiligentRobotics',
    'droneup': 'droneup',
    'dusty robotics': 'DustyRobotics',
    'eleutherai': 'EleutherAI',
    'emergence ai': 'EmergenceAI',
    'energyx': 'energyx',
    'enthought': 'enthought',
    'extropic': 'extropic-ai',
    'factory ai': 'Factory-AI',
    'fal ai': 'fal-ai',
    'falkonry': 'Falkonry',
    'farama foundation': 'Farama-Foundation',
    'farm-ng': 'farm-ng',
    'farmwise': 'FarmWise',
    'featureform': 'featureform',
    'figure ai': 'figure',
    'firecrawl': 'firecrawl',
    'flytrex': 'Flytrex',
    'formant': 'FormantIO',
    'fort robotics': 'FORT-Robotics',
    'foundation robotics': 'foundationrobotics',
    'foxglove': 'foxglove',
    'gecko robotics': 'GeckoRobotics',
    'generalist ai': 'generalist-ai',
    'glean': 'glean',
    'graze': 'graze',
    'groq research': 'groq',
    'guardian agriculture': 'Guardian-Agriculture',
    'harvey legal': 'harveyai',
    'helicone': 'Helicone',
    'hello robot': 'hello-robot',
    'higgsfield': 'higgsfield-ai',
    'hugging face': 'huggingface',
    'hugging face research': 'huggingface',
    'humanloop': 'humanloop',
    'hume ai': 'HumeAI',
    'imbue': 'imbue-ai',
    'imbue agents': 'imbue-ai',
    'imbue research': 'imbue-ai',
    'induced ai': 'inducedai',
    'infleqtion': 'Infleqtion',
    'inorbit': 'inorbit-ai',
    'intrinsic': 'intrinsic-ai',
    'inworld ai': 'inworld-ai',
    'ionq': 'ionq',
    'iron ox': 'iron-ox',
    'isee': 'iSEE',
    'joby aviation': 'joby-aviation',
    'kalshi markets': 'Kalshi',
    'kapwing': 'kapwing',
    'kodiak robotics': 'KodiakRobotics',
    'krea': 'krea-ai',
    'kumo ai': 'kumo-ai',
    'labelbox': 'Labelbox',
    'lancedb': 'lancedb',
    'langchain': 'langchain-ai',
    'layer ai': 'layerai',
    'left hand robotics': 'LeftHandRobotics',
    'lightmatter': 'Lightmatter',
    'lightricks': 'Lightricks',
    'lindy ai': 'lindy-ai',
    'linear': 'linear',
    'livekit': 'livekit',
    'locus robotics': 'locusrobotics',
    'luma ai': 'lumalabs',
    'luma ai labs': 'lumalabs',
    'luminar': 'luminartech',
    'machina labs': 'Machinalabs',
    'magic': 'magic',
    'martian': 'withmartian',
    'mat3ra': 'mat3ra',
    'matternet': 'matternet',
    'matterport': 'matterport',
    'midjourney': 'midjourney',
    'minion ai': 'minionai',
    'miso robotics': 'MisoRobotics',
    'modern treasury': 'Modern-Treasury',
    'modular ai': 'modular',
    'motional': 'motional',
    'mytra': 'MytraAI',
    'nauto': 'nauto',
    'netlify': 'netlify',
    'netradyne': 'netradyne',
    'niantic spatial': 'nianticspatial',
    'nimble ai': 'nimblerobotics',
    'nimble robotics': 'nimblerobotics',
    'normal computing': 'normal-computing',
    'northflank': 'northflank',
    'nous research': 'NousResearch',
    'numerai': 'numerai',
    'nuro': 'nuro-ai',
    'open robotics': 'ros',
    'openart': 'OpenArt-AI',
    'openevidence': 'openevidence',
    'orbital materials': 'orbital-materials',
    'osaro': 'OsaroAI',
    'otoy': 'OTOY',
    'parallel web': 'parallel-web',
    'parasail': 'parasail-ai',
    'path robotics': 'path-robotics',
    'pdt partners': 'pdtpartners',
    'peak energy': 'Peak-Energy',
    'perplexity ai': 'perplexityai',
    'perplexity vision': 'perplexityai',
    'physical intelligence': 'Physical-Intelligence',
    'plaid': 'plaid',
    'planet labs': 'planetlabs',
    'polycam': 'PolyCam',
    'polymath robotics': 'polymathrobotics',
    'poolside': 'poolsideai',
    'poolside research': 'poolsideai',
    'portkey': 'Portkey-AI',
    'positron ai': 'positron-ai',
    'postera': 'postera-ai',
    'prime intellect': 'PrimeIntellect-ai',
    'primer ai': 'PrimerAI',
    'quera computing': 'QuEraComputing',
    'radical ai materials': 'Radical-AI',
    'rapid robotics': 'rapidrobotics',
    'ray project': 'ray-project',
    'raycast': 'raycast',
    'ready robotics': 'ready-robotics',
    'realtime robotics': 'RealtimeRobotics',
    'recogni': 'recogni',
    'redwood materials': 'redwoodmaterials',
    'reka ai': 'reka-ai',
    'relational ai': 'RelationalAI',
    'reliable robotics': 'reliable-ai',
    'replicate diffusion': 'replicate',
    'replit': 'replit',
    'rerun': 'rerun-io',
    'resemble ai': 'resemble-ai',
    'retell ai': 'RetellAI',
    'rigetti computing': 'rigetti',
    'righthand robotics': 'RightHandRobotics',
    'rios intelligent machines': 'rios',
    'roboflow': 'roboflow',
    'rosebud ai': 'rosebudai',
    'rowan scientific': 'rowansci',
    'runpod': 'runpod',
    'runway': 'runwayml',
    'runway gen': 'runwayml',
    'runway research': 'runwayml',
    'sabanto': 'sabantoag',
    'sambanova': 'sambanova',
    'samsara': 'samsara',
    'sardine': 'sardine-ai',
    'scale ai': 'scaleapi',
    'scythe robotics': 'scythe-robotics',
    'second front': 'second-front',
    'sf compute': 'sfcompute',
    'shield ai': 'shield-ai',
    'sierra research': 'sierra-research',
    'sigma computing': 'sigmacomputing',
    'simbe robotics': 'SimbeRobotics',
    'sketchfab': 'sketchfab',
    'skild ai': 'skild-ai',
    'skydio': 'Skydio',
    'skyvern': 'Skyvern-AI',
    'sloyd': 'Sloydai',
    'snorkel ai': 'snorkel-ai',
    'soft robotics': 'SoftRobotics',
    'sourcegraph': 'sourcegraph',
    'sri international': 'SRI-International',
    'standard bots': 'standardbots',
    'standard cyborg': 'StandardCyborg',
    'suno': 'suno-ai',
    'supabase': 'supabase',
    'surge ai': 'surge-ai',
    'sweep ai': 'sweepai',
    'symbolica': 'symbolica-ai',
    'tangram vision': 'Tangram-Vision',
    'tecton': 'tecton-ai',
    'tenstorrent': 'tenstorrent',
    'terra praxis': 'TerraPraxis',
    'threekit': 'Threekit',
    'topaz labs': 'TopazLabs',
    'torc robotics': 'torc-ai',
    'tortoise': 'tortoise',
    'toyota research institute': 'ToyotaResearchInstitute',
    'turbopuffer': 'turbopuffer',
    'two sigma': 'twosigma',
    'ultralytics': 'ultralytics',
    'union ai': 'unionai',
    'uptake': 'uptake',
    'val town': 'val-town',
    'vannevar labs': 'vannevar-labs',
    'vapi': 'VapiAI',
    'vecna robotics': 'vecnarobotics',
    'vellum': 'vellum-ai',
    'vercel ai': 'vercel',
    'verdant robotics': 'Verdant-Robotics',
    'viam': 'viamrobotics',
    'viam robotics': 'viamrobotics',
    'volinga': 'Volinga',
    'voltage park': 'voltagepark',
    'voxel51': 'voxel51',
    'waymo': 'waymo-research',
    'weights biases': 'wandb',
    'wonder dynamics': 'WonderDynamics',
    'xwing': 'xwingai',
    'zed industries': 'zed-industries',
}


def resolve_org(client: Client, company: str, domain: str,
                skip_search: bool = False) -> tuple[str | None, str]:
    """Find the org whose homepage matches the domain. Falls back to the stem."""
    override = ORG_OVERRIDES.get(company.strip().lower())
    if override:
        detail, status = client.get(f"/orgs/{override}")
        if status == "throttled":
            return None, "throttled"
        if status == "ok" and isinstance(detail, dict):
            return detail.get("login"), "ok"

    stem = domain.split(".")[0]
    detail, status = client.get(f"/orgs/{stem}")
    if status == "throttled":
        return None, "throttled"
    if status == "ok" and isinstance(detail, dict):
        return detail.get("login"), "ok"

    # The search fallback costs a search-quota call plus up to five detail calls
    # per company, and search is capped at 30/minute -- on a 700-account sweep it
    # dominates the wall clock. Measured yield beyond ORG_OVERRIDES and the domain
    # stem: 3 orgs out of 220 companies. --skip-search trades that tail for speed.
    if skip_search:
        return None, "no_org_found"

    q = urllib.parse.quote(f"{company} type:org")
    res, status = client.get(f"/search/users?q={q}&per_page=5")
    if status == "throttled":
        return None, "throttled"
    for item in (res or {}).get("items", []) if isinstance(res, dict) else []:
        detail, status = client.get(item["url"])
        if status == "throttled":
            return None, "throttled"
        if isinstance(detail, dict) and domain in (detail.get("blog") or "").lower():
            return detail.get("login"), "ok"
    return None, "no_org_found"


def harvest_domain(client: Client, company: str, domain: str, *,
                   repos: int = 4, per_repo: int = 100,
                   skip_search: bool = False) -> DomainResult:
    out = DomainResult(company=company, domain=domain)
    org, status = resolve_org(client, company, domain, skip_search=skip_search)
    if status == "throttled":
        out.status = "throttled"
        return out
    if not org:
        out.status = "no_org_found"
        return out
    out.org = org

    repo_list, status = client.get(f"/orgs/{org}/repos?per_page={repos}&sort=pushed")
    if status == "throttled":
        out.status = "throttled"
        return out
    if not isinstance(repo_list, list):
        out.status = "no_public_repos"
        return out
    if not repo_list:
        out.status = "no_public_repos"
        return out

    saw_commits = False
    out.repos_seen = len(repo_list[:repos])
    out.archived_repos = sum(1 for r in repo_list[:repos] if r.get("archived"))
    for repo in repo_list[:repos]:
        commits, status = client.get(
            f"/repos/{repo['full_name']}/commits?per_page={per_repo}")
        if status == "throttled":
            out.status = "throttled" if not out.addresses else "partial_throttled"
            return out
        if not isinstance(commits, list):
            continue
        saw_commits = True
        for c in commits:
            author = (c.get("commit") or {}).get("author") or {}
            email = (author.get("email") or "").strip().lower()
            name = (author.get("name") or "").strip()
            when = author.get("date") or ""
            if not email.endswith("@" + domain):
                continue
            if not is_person_address(email, name):
                out.filtered += 1
                continue
            prev = out.addresses.get(email)
            if not prev or when > prev[1]:
                out.addresses[email] = (name, when)

    out.status = "ok" if out.addresses else ("no_addresses" if saw_commits else "no_public_repos")
    return out


# ------------------------------------------------------------------ patterns


PATTERNS = {
    "first": lambda f, l: f,
    "first.last": lambda f, l: f"{f}.{l}",
    "firstlast": lambda f, l: f"{f}{l}",
    "flast": lambda f, l: f"{f[0]}{l}" if f else "",
    "f.last": lambda f, l: f"{f[0]}.{l}" if f else "",
    "firstl": lambda f, l: f"{f}{l[0]}" if l else "",
    "last": lambda f, l: l,
}


def infer_pattern(addresses: dict[str, tuple[str, str]]) -> tuple[str | None, float, list[str]]:
    """Infer the local-part convention from observed name/address pairs.

    This is the payoff. One confirmed convention turns every name found elsewhere
    into a candidate address, which is most of what discovery is for.
    """
    votes: Counter[str] = Counter()
    used: list[str] = []
    for email, (name, _when) in addresses.items():
        local = email.split("@", 1)[0]
        parts = [p for p in re.split(r"[^a-z]+", (name or "").lower()) if len(p) > 1]
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        matched = [p for p, fn in PATTERNS.items() if fn(first, last) == local]
        if matched:
            used.append(email)
        for p in matched:
            votes[p] += 1
    if not votes:
        return None, 0.0, used
    pattern, n = votes.most_common(1)[0]
    return pattern, n / max(1, len(used)), used


def apply_pattern(pattern: str, full_name: str, domain: str) -> str | None:
    parts = [p for p in re.split(r"[^a-z]+", full_name.lower()) if len(p) > 1]
    if len(parts) < 2 or pattern not in PATTERNS:
        return None
    local = PATTERNS[pattern](parts[0], parts[-1])
    return f"{local}@{domain}" if local else None
