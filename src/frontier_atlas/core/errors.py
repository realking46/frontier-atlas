"""Error taxonomy shared by fetchers and LLM providers.

The orchestrator decides what to do purely from the exception *type*:
  RateLimitError      -> back off (honour Retry-After), then fall through to next provider
  PayloadTooLargeError -> shrink the payload and retry the same provider
  TransientError      -> back off and retry the same provider
  InvalidOutputError  -> one repair attempt, then next provider
  FatalProviderError  -> disable provider for this process (bad key, model gone)
"""
from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, message: str, *, provider: str = "", status: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status = status


class RateLimitError(ProviderError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class PayloadTooLargeError(ProviderError):
    """HTTP 413, or a 400 that says the context window was exceeded."""


class TransientError(ProviderError):
    """5xx, timeouts, connection resets."""


class InvalidOutputError(ProviderError):
    """Model answered, but not with valid JSON matching the schema."""


class FatalProviderError(ProviderError):
    """401/403/404 – retrying will never help."""


class NoEntityFoundError(Exception):
    """The model looked at the page and found no entity of the requested type.
    This is a valid answer, not a failure – the fallback chain stops here."""


class AllProvidersFailedError(Exception):
    def __init__(self, errors: list[tuple[str, Exception]]):
        self.errors = errors
        summary = "; ".join(f"{p}: {type(e).__name__}: {e}" for p, e in errors)
        super().__init__(f"all LLM providers failed -> {summary}")
