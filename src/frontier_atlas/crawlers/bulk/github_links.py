"""Find, normalise and choose GitHub repository links for a paper."""
from __future__ import annotations

import re

GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]{1,100})",
    re.I,
)
# Path segments that look like owner names but are GitHub site sections.
RESERVED_OWNERS = {
    "features", "topics", "orgs", "sponsors", "about", "marketplace", "collections",
    "trending", "settings", "notifications", "login", "join", "explore", "site", "apps",
    "users", "enterprise", "pricing", "readme", "security", "customer-stories",
}
# Infrastructure repos that pages link to but which are almost never "the paper's code".
FRAMEWORK_REPOS = {
    "huggingface/transformers", "huggingface/diffusers", "huggingface/peft", "huggingface/trl",
    "huggingface/datasets", "huggingface/accelerate", "vllm-project/vllm", "sgl-project/sglang",
    "pytorch/pytorch", "tensorflow/tensorflow", "google/jax", "jax-ml/jax", "microsoft/deepspeed",
    "nvidia/megatron-lm", "ggerganov/llama.cpp", "ggml-org/llama.cpp", "ollama/ollama",
    "openai/openai-python", "langchain-ai/langchain", "facebookresearch/fairseq",
    "thudm/slime", "pytorch/vision", "lm-sys/fastchat", "eleutherai/lm-evaluation-harness",
}


def normalize_repo(owner: str, name: str) -> str | None:
    owner = owner.strip().lower()
    name = name.strip().rstrip(".,;:)]}>'\"").removesuffix(".git").lower()
    if not owner or not name or owner in RESERVED_OWNERS or name in {".", ".."}:
        return None
    return f"{owner}/{name}"


def repo_url(slug: str) -> str:
    return f"https://github.com/{slug}"


def find_repos(text: str) -> list[str]:
    """All distinct repo slugs (owner/name) in order of first appearance."""
    seen: dict[str, None] = {}
    for owner, name in GITHUB_RE.findall(text or ""):
        slug = normalize_repo(owner, name)
        if slug:
            seen.setdefault(slug, None)
    return list(seen)


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 3}


def choose_repo(candidates: list[str], title: str = "") -> str | None:
    """Pick the repo most likely to be the paper's own code.

    Score = overlap between repo name tokens and title tokens (incl. the title's
    leading acronym, e.g. "PlayCraft: ..." -> "playcraft"), minus a large penalty
    for well-known framework repos. Ties keep the first-mentioned repo.
    """
    if not candidates:
        return None
    title_toks = _tokens(title)
    head = title.split(":")[0].strip().lower()
    if head and len(head) <= 40:
        title_toks.add(re.sub(r"[^a-z0-9]", "", head))

    def score(i_slug: tuple[int, str]) -> tuple[float, int]:
        i, slug = i_slug
        owner, name = slug.split("/", 1)
        s = 0.0
        if slug in FRAMEWORK_REPOS:
            s -= 10
        name_toks = _tokens(name) | {re.sub(r"[^a-z0-9]", "", name)}
        s += 3 * len(name_toks & title_toks)
        s += 1 * len(_tokens(owner) & title_toks)
        return (s, -i)

    best = max(enumerate(candidates), key=score)
    # If every candidate is a framework repo, we have no paper repo.
    if best[1] in FRAMEWORK_REPOS:
        return None
    return best[1]
