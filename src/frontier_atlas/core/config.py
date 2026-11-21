"""Central settings, loaded from environment variables / .env.

Every tunable that changes between laptop and cluster lives here, so scaling
is a config change, not a code change.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="FA_"
    )

    # --- general ---
    env: str = "dev"
    log_level: str = "INFO"
    log_json: bool = False
    schema_version: str = "1.0"

    # --- LLM provider keys (read without the FA_ prefix as well) ---
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    groq_api_key: str | None = Field(default=None, validation_alias="GROQ_API_KEY")
    deepseek_api_key: str | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")

    # --- LLM models ---
    gemini_model: str = "gemini-2.5-flash"
    groq_model: str = "llama-3.3-70b-versatile"
    deepseek_model: str = "deepseek-chat"

    # Order of the fallback chain, comma separated provider names.
    llm_chain: str = "gemini,groq,deepseek"

    # --- per-provider limits (requests/min, tokens/min, input token budget) ---
    gemini_rpm: int = 15
    gemini_tpm: int = 1_000_000
    gemini_max_input_tokens: int = 30_000
    groq_rpm: int = 30
    groq_tpm: int = 12_000
    groq_max_input_tokens: int = 6_000
    deepseek_rpm: int = 60
    deepseek_tpm: int = 500_000
    deepseek_max_input_tokens: int = 24_000

    # Hard byte ceiling for any request body (guards against HTTP 413 at the gateway).
    llm_max_request_bytes: int = 200_000

    # --- orchestration ---
    llm_concurrency: int = 16
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 4          # per provider, for 5xx / timeouts
    llm_max_429_retries: int = 2      # per provider; after this we fall through to the next provider
    llm_max_shrinks: int = 3          # per provider, for 413 / context overflow
    llm_shrink_factor: float = 0.55   # budget multiplier after each 413
    llm_backoff_base_s: float = 1.0
    llm_backoff_cap_s: float = 60.0
    llm_provider_cooldown_s: float = 90.0  # how long a provider is skipped after exhausting 429 retries

    # --- crawling ---
    user_agent: str = "FrontierAtlasBot/0.1 (+research data pipeline)"
    http_timeout_s: float = 30.0
    http_max_connections: int = 100
    http_per_host_concurrency: int = 8
    http_max_retries: int = 5
    # minimum seconds between requests to one host (politeness); per-host overrides below
    http_min_interval_s: float = 0.0
    arxiv_min_interval_s: float = 3.0   # arXiv API terms: 1 request / 3 s
    pwc_min_interval_s: float = 1.0

    # --- protected sites (core/protected.py) ---
    robots_enabled: bool = True
    browser_enabled: bool = True          # Playwright tier; started lazily on first escalation
    browser_max_pages: int = 3
    browser_challenge_wait_s: float = 15.0
    breaker_threshold: int = 3            # consecutive blocks before a host's circuit opens
    breaker_cooldown_s: float = 600.0     # doubles on each re-opening

    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    db_path: str = "data/frontier_atlas.sqlite3"
    seen_db_path: str = "data/seen.sqlite3"       # freshness / exactly-once state
    freshness_window_hours: float = 24.0
    node_id: str = "node-0"
    output_dir: str = "data/output"

    # --- Google Sheets deliverable (export/sheets.py) ---
    google_sheet_id: str | None = None                    # the id in docs.google.com/spreadsheets/d/<id>/
    google_service_account_file: str | None = None        # service-account JSON key; keep it in secrets/

    # --- grounding (anti-hallucination) ---
    grounding_min_score: int = 85     # rapidfuzz partial_ratio threshold 0-100

    @property
    def chain(self) -> list[str]:
        return [p.strip().lower() for p in self.llm_chain.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
