"""Recognise bot-protection responses and decide what to do about them.

Policy (deliberate, see architecture doc):

    kind            example                                   action
    ----            -------                                   ------
    RATE_LIMITED    429, plain 503                            back off (HttpFetcher), slow the host down
    JS_CHALLENGE    Cloudflare "Just a moment" (cf-mitigated)  escalate to a real browser; non-interactive
                                                              challenges complete on their own in one
    JS_REQUIRED     200 but an empty app shell                 escalate to a real browser (render the page)
    WAF_DENIED      bare 403/405 with no vendor page           one browser attempt, then give up on the host
    CAPTCHA         DataDome, AWS WAF captcha, hCaptcha,       STOP. We never solve or outsource captchas:
                    interactive Turnstile                     open the host's circuit, use an alternative
                                                              channel (RSS / API / sitemap) instead
    HARD_BLOCK      Cloudflare "Attention Required" / 1020     STOP, same as CAPTCHA

A protection system saying "prove you are human" is the site owner's answer
to automated access; the pipeline respects it and routes around it through
the channels the site publishes for machines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from collections.abc import Mapping


class BlockKind(str, Enum):
    NONE = "none"
    RATE_LIMITED = "rate_limited"
    JS_CHALLENGE = "js_challenge"
    JS_REQUIRED = "js_required"
    WAF_DENIED = "waf_denied"
    CAPTCHA = "captcha"
    HARD_BLOCK = "hard_block"
    ROBOTS = "robots_disallowed"

    @property
    def browser_may_help(self) -> bool:
        return self in (BlockKind.JS_CHALLENGE, BlockKind.JS_REQUIRED, BlockKind.WAF_DENIED)

    @property
    def terminal(self) -> bool:
        """Nothing we are willing to do will get past it."""
        return self in (BlockKind.CAPTCHA, BlockKind.HARD_BLOCK, BlockKind.ROBOTS)


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    vendor: str = ""
    detail: str = ""

    def __bool__(self) -> bool:
        return self.kind is not BlockKind.NONE


NO_BLOCK = Block(BlockKind.NONE)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CAPTCHA_MARKS = ("captcha-delivery.com", "geo.captcha-delivery", "hcaptcha.com/1/api", "px-captcha",
                  "g-recaptcha", "awswaf", "captcha.awswaf", "cf-turnstile-response")
_SHELL_MARKS = ("enable javascript", "you need to enable javascript", "requires javascript",
                "please turn javascript on", "javascript is disabled")


def _title(text: str) -> str:
    m = _TITLE_RE.search(text[:20000])
    return re.sub(r"\s+", " ", m.group(1)).strip().lower() if m else ""


def _visible_len(text: str) -> int:
    body = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", text)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", body).strip())


def classify(status: int, headers: Mapping[str, str], text: str) -> Block:
    h = {k.lower(): v for k, v in headers.items()}
    low = text[:60000].lower()
    title = _title(text)
    server = h.get("server", "").lower()

    if status == 429:
        return Block(BlockKind.RATE_LIMITED, server, "429")

    # ---- vendor-specific, strongest signals first
    if "x-datadome" in h or "captcha-delivery.com" in low:
        if status >= 400:
            return Block(BlockKind.CAPTCHA, "datadome", h.get("x-datadome", ""))
    if h.get("x-amzn-waf-action", "").lower() in ("captcha", "challenge"):
        return Block(BlockKind.CAPTCHA, "aws-waf", h["x-amzn-waf-action"])
    if h.get("x-amzn-waf-action", "").lower() == "block":
        return Block(BlockKind.HARD_BLOCK, "aws-waf", "block")
    if h.get("cf-mitigated", "").lower() == "challenge" or title.startswith("just a moment"):
        return Block(BlockKind.JS_CHALLENGE, "cloudflare", title or "cf-mitigated")
    if "cloudflare" in server and status in (403, 503):
        if title.startswith("attention required") or "cf-error-code" in low or "error code: 1020" in low:
            return Block(BlockKind.HARD_BLOCK, "cloudflare", title)
    if "_incapsula_resource" in low or "incapsula incident" in low:
        return Block(BlockKind.CAPTCHA if status >= 400 else BlockKind.JS_CHALLENGE, "imperva", "")
    if status in (401, 403, 405, 406, 451):
        if any(m in low for m in _CAPTCHA_MARKS) or "human verification" in title or "captcha" in title:
            return Block(BlockKind.CAPTCHA, server or "unknown", title)
        return Block(BlockKind.WAF_DENIED, server or "unknown", f"http {status}")
    if status == 503:          # vendor challenges on 503 were caught above; this is overload
        return Block(BlockKind.RATE_LIMITED, server, "503")

    # ---- 200 responses that are not the content
    if status == 200:
        if any(m in title for m in ("human verification", "verify you are human", "are you a robot")):
            return Block(BlockKind.CAPTCHA, server or "unknown", title)
        if len(text) < 60000 and _visible_len(text) < 200 and (
                any(m in low for m in _SHELL_MARKS) or re.search(r'<div id="(root|app|__next)"[^>]*>\s*</div>', low)):
            return Block(BlockKind.JS_REQUIRED, "", "empty app shell")
    return NO_BLOCK
