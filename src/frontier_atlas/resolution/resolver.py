"""Deterministic entity resolution for startup / product / company names.

Seed matching, strongest evidence first (first hit wins):

    domain        company website domain matches a seed domain          score 100
    exact         case-insensitive cleaned name == seed name/alias      score 100
    legal         equal after removing legal suffixes ("OpenAI, Inc.")  score 100
    compact       equal ignoring case/spacing/punctuation ("Open AI")   score 98
    noise         equal after dropping slug artefacts ("…linkedin")     score 95
    descriptor    equal without generic words ("Cohere Technologies")   score 93
                  – only for distinctive keys (>= 5 chars, not a common word, unique)
    fuzzy         Levenshtein ratio on compact keys >= 92, len >= 6, same first
                  letter, and >= 4 points ahead of the runner-up        score = ratio
    (descriptor and fuzzy are skipped when the name carries its own website
     domain that is not the seed's - approximate name + different site = different company)

Names that match no seed become *new canonical entities* ("dynamic
canonicalisation"): they are clustered by noise key, then near-identical keys
are merged (ratio >= 94, len >= 8) - unless both carry different website
domains, or they differ only in digits (see _may_be_typo). Each cluster's display name is chosen by a
fixed rule – a website-backed spelling first, then the most frequent legal-stripped form, then mixed case over ALL-CAPS /
lowercase, then shorter, then alphabetical – so the output depends only on the
*set* of names seen, never on crawl order.
"""
from __future__ import annotations

import re

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from .normalizer import NameKeys, domain_of, keys

DEFAULT_SEEDS = Path(__file__).resolve().parents[3] / "data" / "seeds" / "canonical_entities.json"

# Distinctive-key guard for descriptor matching: these are ordinary words.
COMMON_WORDS = {
    "open", "scale", "figure", "runway", "writer", "harvey", "sierra", "glean", "cognition", "lambda",
    "poolside", "stability", "inflection", "moonshot", "together", "character", "magic", "data", "deep",
    "black", "physical", "safe", "thinking", "weights", "core", "mini", "eleven", "mid", "any",
}


@dataclass(frozen=True, slots=True)
class SeedEntity:
    id: str
    name: str
    aliases: tuple[str, ...]
    domains: tuple[str, ...]
    strict_text: bool = False


@dataclass(frozen=True, slots=True)
class Resolution:
    raw: str
    canonical: str
    canonical_id: str     # seed id, or "new:<key>"
    method: str
    score: float
    is_new: bool


def load_seeds(path: str | Path = DEFAULT_SEEDS) -> list[SeedEntity]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        SeedEntity(e["id"], e["name"], tuple(e.get("aliases", [])), tuple(e.get("domains", [])),
                   bool(e.get("strict_text", False)))
        for e in data["entities"]
    ]


class EntityResolver:
    def __init__(self, seeds: list[SeedEntity] | None = None):
        self.seeds = seeds if seeds is not None else load_seeds()
        self.by_id = {s.id: s for s in self.seeds}
        self._exact: dict[str, str] = {}
        self._legal: dict[str, str] = {}
        self._compact: dict[str, str] = {}
        self._noise: dict[str, str] = {}
        self._domain: dict[str, str] = {}
        desc: defaultdict[str, set[str]] = defaultdict(set)
        for s in self.seeds:
            for d in s.domains:
                self._domain[d.lower()] = s.id
            for form in (s.name, *s.aliases):
                k = keys(form)
                self._exact.setdefault(k.clean.lower(), s.id)
                self._legal.setdefault(k.core.lower(), s.id)
                self._compact.setdefault(k.compact, s.id)
                self._noise.setdefault(k.noise, s.id)
                desc[k.descriptor].add(s.id)
        # descriptor keys: distinctive and unambiguous only
        self._descriptor = {
            dk: next(iter(ids)) for dk, ids in desc.items()
            if len(ids) == 1 and len(dk) >= 5 and dk not in COMMON_WORDS
        }
        self._fuzzy_keys = sorted(self._compact)

    # ------------------------------------------------------------------ seed matching
    def match_seed(self, raw: str, *, domain: str | None = None) -> Resolution | None:
        if not raw or not raw.strip():
            return None
        k = keys(raw)
        d = domain_of(domain)
        if d:
            for cand in (d, ".".join(d.split(".")[-2:])):   # sub.domain.com -> domain.com
                if cand in self._domain:
                    return self._hit(raw, self._domain[cand], "domain", 100)
        for table, key, method, score in (
            (self._exact, k.clean.lower(), "exact", 100),
            (self._legal, k.core.lower(), "legal", 100),
            (self._compact, k.compact, "compact", 98),
            (self._noise, k.noise, "noise", 95),
            (self._descriptor, k.descriptor, "descriptor", 93),
        ):
            if key and key in table:
                if d and method == "descriptor":
                    break       # approximate match vs. a website that is not the seed's: different company
                return self._hit(raw, table[key], method, score)
        if d:
            # The name comes with its own website and it is not the seed's ("Cognitio Labs",
            # cognitiolabs.com vs Cognition). Only exact-name forms may override that evidence.
            return None
        return self._fuzzy(raw, k)

    def _fuzzy(self, raw: str, k: NameKeys) -> Resolution | None:
        if len(k.compact) < 6:
            return None
        top = process.extract(k.compact, self._fuzzy_keys, scorer=fuzz.ratio, limit=2)
        if not top:
            return None
        best_key, best, _ = top[0]
        runner = top[1][1] if len(top) > 1 else 0
        if best >= 92 and best_key[0] == k.compact[0] and best - runner >= 4:
            return self._hit(raw, self._compact[best_key], "fuzzy", round(best, 1))
        return None

    def _hit(self, raw: str, seed_id: str, method: str, score: float) -> Resolution:
        return Resolution(raw, self.by_id[seed_id].name, seed_id, method, score, False)

    def split_by_site(self, res: Resolution, raw: str, site: str | None) -> Resolution:
        """`site` is the entity's OWN website (a startup profile's homepage). If the name
        matched a seed by spelling only, but the seed lives on other domains, it is a
        different company that shares the name (YC "Cohere" at cohere.io is not Cohere at
        cohere.com) and becomes its own entity."""
        d = domain_of(site)
        if res.is_new or res.method == "domain" or not d:
            return res
        seed_domains = self.by_id[res.canonical_id].domains
        if not seed_domains:
            return res
        k = keys(raw)
        return Resolution(raw, k.core, f"new:{k.noise or k.compact}@{d}", "new-site-conflict", 100.0, True)

    # ------------------------------------------------------------------ batch (with new entities)
    def resolve_batch(self, names: Iterable[str | tuple[str, str | None]]) -> dict[str, Resolution]:
        """Resolve many raw names at once. Items may be `name` or `(name, domain)`.
        Deterministic: the result depends only on the multiset of inputs."""
        counts: Counter[tuple[str, str | None]] = Counter()
        for item in names:
            name, dom = (item, None) if isinstance(item, str) else item
            if name and name.strip():
                counts[(name, dom)] += 1

        out: dict[str, Resolution] = {}
        unmatched: Counter[str] = Counter()
        domains_of: defaultdict[str, set[str]] = defaultdict(set)
        # occurrences that carry a domain first, so a name is labelled by its strongest evidence
        for (name, dom), n in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or "")):
            if name in out and not out[name].is_new:
                continue
            hit = self.match_seed(name, domain=dom)
            if hit:
                out[name] = hit
            else:
                unmatched[name] += n
                if dom:
                    domains_of[name].add(dom)
        for name in [n for n in unmatched if n in out]:   # matched via another occurrence's domain
            del unmatched[name]

        # cluster unknown names by noise key
        clusters: defaultdict[str, Counter[str]] = defaultdict(Counter)
        cluster_domains: defaultdict[str, set[str]] = defaultdict(set)
        for name, n in unmatched.items():
            k = keys(name)
            if k.noise:
                clusters[k.noise][name] += n
                cluster_domains[k.noise] |= domains_of[name]

        # merge near-identical cluster keys (typos), biggest clusters first
        order = sorted(clusters, key=lambda ck: (-sum(clusters[ck].values()), ck))
        rep_of: dict[str, str] = {}
        reps: list[str] = []
        for ck in order:
            target = None
            if len(ck) >= 8:
                for r in reps:
                    if (r[:2] == ck[:2] and abs(len(r) - len(ck)) <= 3 and fuzz.ratio(r, ck) >= 94
                            and _may_be_typo(r, ck, cluster_domains[r], cluster_domains[ck])):
                        target = r
                        break
            if target is None:
                reps.append(ck)
                rep_of[ck] = ck
            else:
                rep_of[ck] = target
        merged: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for ck, names_ in clusters.items():
            merged[rep_of[ck]].update(names_)

        for rep, names_ in merged.items():
            canonical = _choose_display(names_, {n for n in names_ if domains_of[n]})
            for name in names_:
                own_key = keys(name).noise
                method = "new" if own_key == rep and keys(name).core == canonical else (
                    "new-merge-fuzzy" if own_key != rep else "new-variant")
                score = 100.0 if own_key == rep else round(fuzz.ratio(own_key, rep), 1)
                out[name] = Resolution(name, canonical, f"new:{rep}", method, score, True)
        return out

    def resolve(self, name: str, domain: str | None = None) -> Resolution:
        """Single name. Unknown names canonicalise to their own cleaned core form."""
        return self.resolve_batch([(name, domain)])[name]


def _may_be_typo(a: str, b: str, doms_a: set[str], doms_b: set[str]) -> bool:
    """Guards for merging two *unknown* names as spelling variants.

    - two names that each come with their own, different website are two companies
      (YC lists both "Caire Health" and "Cair Health"), however similar the strings;
    - names that differ only in digits are versions / distinct brands ("Fountain9" vs "Fountain").
    """
    if doms_a and doms_b and not (doms_a & doms_b):
        return False
    if re.sub(r"\d", "", a) == re.sub(r"\d", "", b):
        return False
    return True


def _display_rank(form: str, n: int, evidenced: bool = False) -> tuple:
    mixed = not (form.isupper() or form.islower())
    return (not evidenced, -n, not mixed, len(form), form)


def _choose_display(names: Counter[str], evidenced: set[str] = frozenset()) -> str:
    """A spelling that came with the company's own website (a directory profile, the
    vendor's homepage) beats unverified spellings; then frequency, then the rules above."""
    forms: Counter[str] = Counter()
    backed: set[str] = set()
    for name, n in names.items():
        core = keys(name).core
        forms[core] += n
        if name in evidenced:
            backed.add(core)
    return min(forms.items(), key=lambda kv: _display_rank(kv[0], kv[1], kv[0] in backed))[0]


@lru_cache(maxsize=1)
def default_resolver() -> EntityResolver:
    return EntityResolver()
