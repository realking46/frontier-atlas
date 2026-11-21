"""Deterministic job classification: AI relevance, role family, remote eligibility.

Rules are transparent (every match is recorded on the record) and need no LLM,
so they are free at 500k scale; the LLM is reserved for fields that need
reading comprehension (company / title on free-form pages).
"""
from __future__ import annotations

import re

# --- AI relevance -----------------------------------------------------------------
# Strong terms: one hit in title/tags is enough. In the description we need two
# distinct terms, because "AI-powered company" boilerplate is everywhere.
AI_TERMS: dict[str, str] = {
    "machine learning": r"machine[- ]learning",
    "deep learning": r"deep[- ]learning",
    "artificial intelligence": r"artificial intelligence",
    "AI": r"(?<![\w/])(?:AI|A\.I\.)(?![\w])",          # case-sensitive: avoids 'said', 'aid'
    "ML": r"(?<![\w/])ML(?![\w])",
    "AI/ML": r"AI\s*/\s*ML|ML\s*/\s*AI",
    "LLM": r"\bLLMs?\b",
    "GenAI": r"\bgen(?:erative)?[- ]?ai\b",
    "NLP": r"\bNLP\b|natural language processing",
    "computer vision": r"computer vision",
    "MLOps": r"\bML ?Ops\b|\bLLM ?Ops\b",
    "data scientist": r"data scien(?:tist|ce)",
    "applied scientist": r"applied scientist",
    "research scientist": r"research scientist",
    "prompt engineer": r"prompt engineer",
    "reinforcement learning": r"reinforcement learning|\bRLHF\b",
    "neural networks": r"neural net",
    "PyTorch": r"\bpytorch\b",
    "TensorFlow": r"\btensorflow\b",
    "transformers": r"\btransformer models?\b|hugging ?face",
    "RAG": r"\bRAG\b|retrieval[- ]augmented",
    "AI agents": r"\bai agents?\b|\bagentic\b",
    "foundation models": r"foundation models?",
    "ML engineer": r"\bml engineer|machine learning engineer",
}
_CASE_SENSITIVE = {"AI", "ML", "AI/ML", "RAG", "NLP", "LLM", "MLOps"}
_COMPILED = {
    k: re.compile(p) if k in _CASE_SENSITIVE else re.compile(p, re.I) for k, p in AI_TERMS.items()
}


# Overlapping terms collapse into one concept, so "AI" + "AI agents" is not two signals.
CONCEPT = {
    "machine learning": "ml", "ML": "ml", "AI/ML": "ml", "ML engineer": "ml",
    "deep learning": "dl", "neural networks": "dl",
    "LLM": "llm", "foundation models": "llm", "transformers": "llm", "RAG": "llm", "prompt engineer": "llm",
    "NLP": "nlp", "computer vision": "cv", "MLOps": "mlops", "reinforcement learning": "rl",
    "data scientist": "ds", "applied scientist": "ds", "research scientist": "research",
    "PyTorch": "frameworks", "TensorFlow": "frameworks",
    # generic – company boilerplate ("our AI-powered platform") is full of these
    "AI": "generic", "artificial intelligence": "generic", "GenAI": "generic", "AI agents": "generic",
}


def ai_matches(text: str) -> list[str]:
    return [k for k, rx in _COMPILED.items() if rx.search(text or "")]


def ai_relevance(title: str, tags: list[str] | None = None, description: str = "") -> tuple[bool, list[str]]:
    """AI job if the *title* or *tags* name an AI term, or the description contains
    at least two distinct technical (non-generic) AI concepts."""
    title_hits = ai_matches(title)
    tag_hits = ai_matches(" | ".join(tags or []))
    desc_hits = ai_matches(description[:20000])
    matched = list(dict.fromkeys(title_hits + tag_hits + desc_hits))
    technical = {CONCEPT[h] for h in desc_hits} - {"generic"}
    relevant = bool(title_hits or tag_hits or len(technical) >= 2)
    return relevant, matched


# --- role family ----------------------------------------------------------------
# Order matters: most specific families first ("Product Manager, AI Platform" is Product,
# "Data Scientist" is Data, not Research).
ROLE_RULES: list[tuple[str, re.Pattern]] = [
    ("Product", re.compile(r"product (manager|owner|lead|director)|\bpm\b|product management", re.I)),
    ("Data", re.compile(r"\bdata\b|\banalytics?\b|\banalyst\b|\bbi\b|statistic|"
                        r"\bevaluator\b|annotat|label(l)?ing|\bai trainer\b|\bai tutor\b", re.I)),
    ("Research", re.compile(r"\bresearch(er)?\b|\bscientist\b|\bphd\b|postdoc", re.I)),
    # before Engineering: "Software Sales Director" is Sales ("Salesforce Developer" is not: \bsales\b)
    ("Sales", re.compile(r"\bsales\b|account (executive|manager)|business development|\bbdr\b|\bsdr\b|"
                         r"partnerships|pre-?sales", re.I)),
    ("Engineering", re.compile(
        r"engineer|developer|\bdev\b|programmer|architect|\bsre\b|devops|ml ?ops|software|backend|"
        r"front[- ]?end|full[- ]?stack|infrastructure|platform|security|\bqa\b|test|technical lead|\bcto\b|"
        r"member of technical staff|\bmts\b|desenvolvedor|desarrollador|d[ée]veloppeur|entwickler|"
        r"ing[ée]nieur|ingeniero|engenheiro",
        re.I)),
    ("Design", re.compile(r"design(er)?\b|\bux\b|\bui\b|creative", re.I)),
    ("Marketing", re.compile(r"marketing|growth|\bseo\b|content|community|brand|communications|\bpr\b", re.I)),
    ("Operations", re.compile(r"operations|\bops\b|recruit|talent|\bhr\b|people|finance|account(ant|ing)|"
                              r"legal|counsel|customer (success|support)|support|admin|office|chief of staff", re.I)),
]


def role_family(title: str) -> str:
    # "Solutions Engineer" is sales-facing; check that before generic "engineer"
    if re.search(r"solutions? (engineer|consultant|architect)|sales engineer", title, re.I):
        return "Sales"
    for family, rx in ROLE_RULES:
        if rx.search(title):
            return family
    return "Other"


# --- remote --------------------------------------------------------------------
REMOTE_RX = re.compile(r"\bremote\b|work from (home|anywhere)|\bwfh\b|anywhere in the world|\bworldwide\b|"
                       r"\bdistributed\b|telecommute", re.I)
ONSITE_RX = re.compile(r"\bon[- ]?site\b|in[- ]office|\bin person\b|no remote", re.I)


def infer_remote(*texts: str | None) -> bool | None:
    joined = " ".join(t for t in texts if t)
    if not joined:
        return None
    if REMOTE_RX.search(joined):
        return True
    if ONSITE_RX.search(joined):
        return False
    return None
