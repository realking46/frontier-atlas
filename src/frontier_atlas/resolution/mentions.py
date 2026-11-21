"""Find canonical companies *mentioned* in free text (news titles / bodies).

Gazetteer matching over seed names + aliases:
  * case-sensitive, whole-word (so "financial stability" never hits Stability AI)
  * entities flagged `strict_text` (common words: Harvey, Writer, Figure, Scale …)
    only match through multi-word / punctuated forms ("Harvey AI", "Scale AI")
  * single-word aliases that are dictionary words are skipped for every entity
  * returns canonical names ordered by first mention, with mention counts
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .resolver import COMMON_WORDS, EntityResolver, default_resolver


@dataclass(frozen=True, slots=True)
class Mention:
    canonical: str
    canonical_id: str
    surface: str      # the form that appeared in the text
    count: int
    first_pos: int


def _is_multiword(form: str) -> bool:
    return bool(re.search(r"[\s.&\-]", form.strip()))


class MentionMatcher:
    def __init__(self, resolver: EntityResolver | None = None):
        self.resolver = resolver or default_resolver()
        surfaces: dict[str, str] = {}
        for s in self.resolver.seeds:
            for form in (s.name, *s.aliases):
                form = form.strip()
                if len(form) < 3:
                    continue
                if s.strict_text and not _is_multiword(form):
                    continue
                if not _is_multiword(form) and form.lower() in COMMON_WORDS:
                    continue
                surfaces.setdefault(form, s.id)
        self._surface_to_id = surfaces
        alts = sorted(surfaces, key=len, reverse=True)       # longest first: "Google DeepMind" before "DeepMind"
        self._rx = re.compile(r"(?<![\w.])(" + "|".join(re.escape(a) for a in alts) + r")(?![\w])")

    def find(self, *texts: str | None) -> list[Mention]:
        text = "\n".join(t for t in texts if t)
        hits: dict[str, list] = {}
        for m in self._rx.finditer(text):
            sid = self._surface_to_id[m.group(1)]
            if sid not in hits:
                hits[sid] = [m.group(1), 0, m.start()]
            hits[sid][1] += 1
        out = [Mention(self.resolver.by_id[sid].name, sid, surf, n, pos) for sid, (surf, n, pos) in hits.items()]
        return sorted(out, key=lambda m: m.first_pos)
