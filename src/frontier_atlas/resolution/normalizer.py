"""Company-name normalisation: the deterministic keys the resolver matches on.

    raw                         clean               core (no legal suffix)   compact          descriptor
    "OpenAI, Inc."           -> "OpenAI, Inc."    -> "OpenAI"              -> "openai"       -> "openai"
    "Open AI"                -> "Open AI"         -> "Open AI"             -> "openai"       -> "open"
    "Cohere Technologies Inc"-> ...               -> "Cohere Technologies" -> "coheretechnologies" -> "cohere"
    "Acme (YC W24)"          -> "Acme"            -> "Acme"                -> "acme"         -> "acme"
    "Artefactlinkedin"       -> ...                                         -> noise key "artefact"

* clean:      unicode NFKC, quotes, whitespace, trailing "(YC W24)", " - Careers", "| Jobs" …
* core:       legal-form suffixes removed repeatedly (Inc, LLC, GmbH, S.A., PBC, Corporation …)
* compact:    lowercase alphanumerics only ("&" -> "and") – makes spacing/punctuation irrelevant
* descriptor: compact without generic descriptor words (AI, Labs, Technologies …); only used
              when the result is distinctive (see resolver)
* noise:      compact without scraping artefacts appended to slugs ("linkedin", "careers" …)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LEGAL_SUFFIXES = [
    "incorporated", "inc", "llc", "l l c", "ltd", "limited", "gmbh", "mbh", "ag", "sa", "s a", "sas", "sarl",
    "srl", "s r l", "spa", "s p a", "bv", "b v", "nv", "n v", "plc", "corp", "corporation", "co", "company",
    "pbc", "lp", "llp", "pty", "pvt", "private limited", "pte", "kk", "oy", "ab", "as", "se", "sl", "ug",
    "gmbh co kg", "co kg", "kg", "holdings inc", "opco", "s de rl de cv", "sa de cv", "ltda", "lda",
]
_LEGAL_RE = re.compile(
    r"(?:[\s,]+(?:&\s*)?(?:" + "|".join(re.escape(s).replace(r"\ ", r"\.?\s*") for s in
                                     sorted(LEGAL_SUFFIXES, key=len, reverse=True)) + r")\.?)+\s*$",
    re.I,
)
DESCRIPTOR_TOKENS = {
    "ai", "a i", "labs", "lab", "technologies", "technology", "tech", "systems", "software", "research",
    "hq", "group", "global", "io", "the", "inc", "intelligence", "computing", "dev",
}
NOISE_SUFFIXES = ("linkedin", "careers", "career", "jobs", "hiring", "recruiting", "official", "team")
_TRAILING_NOISE_RE = re.compile(
    r"\s*(\((?:YC|Y Combinator)\s*[A-Z]?\d{2,4}\)|\((?:formerly|fka)[^)]*\)|[\-|–—:]\s*(careers?|jobs|hiring|official)\b.*)$",
    re.I,
)


def clean(raw: str) -> str:
    s = unicodedata.normalize("NFKC", raw or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s).strip().strip("\"'").strip()
    s = _TRAILING_NOISE_RE.sub("", s).strip()
    return s


def strip_legal(name: str) -> str:
    core = _LEGAL_RE.sub("", name).strip(" ,.-")
    return core or name


def compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", name.lower()).replace("&", "and"))


def descriptor_key(core: str) -> str:
    tokens = [t for t in re.split(r"[^a-z0-9]+", core.lower()) if t]
    kept = [t for t in tokens if t not in DESCRIPTOR_TOKENS]
    return "".join(kept) if kept else "".join(tokens)


def noise_key(key: str) -> str:
    for suf in NOISE_SUFFIXES:
        if key.endswith(suf) and len(key) - len(suf) >= 4:
            return key[: -len(suf)]
    return key


@dataclass(frozen=True, slots=True)
class NameKeys:
    raw: str
    clean: str
    core: str          # display form without legal suffix
    compact: str
    descriptor: str
    noise: str


def keys(raw: str) -> NameKeys:
    c = clean(raw)
    core = strip_legal(c)
    comp = compact(core)
    return NameKeys(raw, c, core, comp, descriptor_key(core), noise_key(comp))


def domain_of(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split(":")[0]
    return s.removeprefix("www.") or None
