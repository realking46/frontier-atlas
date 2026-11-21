# Phase V: protected and JavaScript-heavy sources

Code: `src/frontier_atlas/core/antibot.py` (classification), `core/protected.py` (tiers, circuit
breaker), `core/browser.py` (Playwright tier), `core/robots.py`.

## Principle

A crawler that has to *defeat* a site's bot protection is in an arms race it loses at scale and
creates legal exposure. The pipeline treats protection as a signal, not an obstacle:

1. **Use the channel the site publishes for machines first**: RSS/Atom, public JSON APIs,
   sitemaps, structured JSON embedded in pages. Most "high-value" data in this project comes this
   way (YC company JSON, Futurepedia's `sitemap_tools.xml`, OpenRouter's API, publisher feeds).
2. **Identify honestly** (`FrontierAtlasBot/0.1` user agent) and **respect robots.txt**, including
   `Crawl-delay`. Observed live: aijobs.net returns 403 to a spoofed browser user agent but serves
   the declared bot.
3. **Render when a page needs a browser**, not to impersonate one: JavaScript-only app shells and
   non-interactive challenges go to real Chromium.
4. **Stop at a human check.** CAPTCHAs (DataDome, AWS WAF, hCaptcha, interactive Turnstile) and
   hard blocks are never solved, outsourced, or evaded. The host's circuit opens and the item is
   routed to an alternative channel or dropped with a recorded reason.

Deliberately **not** built: CAPTCHA-solving services, fingerprint-spoofing or "stealth" patches,
residential-proxy rotation, TLS impersonation. For sources whose terms forbid automated access
(Crunchbase, G2, Glassdoor), the production answer is their licensed API or a data partnership.

## How a fetch works

```
robots.txt ── disallowed ──► Blocked(robots)                 (never fetched)
    │ allowed (+ Crawl-delay → host min interval)
    ▼
HTTP tier (httpx, per-host politeness, 429/5xx backoff with jitter)
    │
classify(status, headers, body)
    ├─ none ────────────────► page
    ├─ rate_limited ────────► transient error, host slowed down
    ├─ js_challenge ─┐
    ├─ js_required ──┼──────► browser tier (sticky for the host) ── classify again
    ├─ waf_denied ───┘                                  ├─ none ──► page
    │                                                   └─ still blocked ──► Blocked
    ├─ captcha ─────────────► Blocked, circuit opens at once
    └─ hard_block ──────────► Blocked, circuit opens at once
```

Per-host state makes this cheap at scale:

| Mechanism | Effect |
|---|---|
| **Probe-first** | The first request to a host runs alone; concurrent requests wait for its outcome, so a CAPTCHA wall is hit once, not N times. |
| **Sticky tier** | Once a host needed the browser, later URLs skip the HTTP attempt that would be challenged anyway. |
| **Circuit breaker** | A CAPTCHA or hard block, or 3 consecutive blocks, opens the circuit for 10 min. The cool-down doubles on each re-opening. |
| **Slow-down** | Each block doubles the host's minimum request interval (up to 60 s): rate is the main input to bot scores. |
| **Browser economy** | One context per site (cookies reused), images, media and fonts blocked, at most 3 pages at once. |

## Signatures (observed live, November 2026)

| Site | Response | Classified as | Handling |
|---|---|---|---|
| theresanaiforthat, toolify, topai.tools, Product Hunt, Glassdoor, Quizlet | 403 + `cf-mitigated: challenge`, "Just a moment..." | js_challenge | Browser tier. Cloudflare still flags headless Chromium, so the result is Blocked and the circuit opens. Product Hunt's RSS feed and toolify's sitemap are open: use those. |
| Crunchbase, Teal, Sapien | 403, "Attention Required! \| Cloudflare" | hard_block | Stop. Licensed API in production. |
| G2 | 403 + `x-datadome`, captcha-delivery script | captcha | Stop. |
| Ars Technica (article HTML) | 405 + `x-amzn-waf-action: captcha` | captcha | Stop. Use the publisher's RSS feed: items kept only when the feed date proves freshness, labelled `text_source=feed-lead`. |
| Wellfound | 200 with page data + an embedded Turnstile widget | none | A widget on a served page is not a block. |
| JS-only vendor sites (e.g. angryemailtranslator.com) | 200, empty `<div id="root">` | js_required | Browser tier renders it: 1.4 KB becomes 9.9 KB of content. |

## In the pipeline

- **News:** article pages go through the protected fetcher. Ars Technica is a sixth, showcase
  source: its article HTML is behind an AWS WAF CAPTCHA, so records carry the publisher's feed
  lead with `text_source=feed-lead`, never presented as full text.
- **Products:**
  ```bash
  python -m frontier_atlas.cli products --retry-makers
  ```
  re-tries the maker lookup for products without one, through the protected fetcher. Run
  summaries include per-host counts by tier and block type (`protected_fetch`).

## Scaling this to 500k

The same per-host state moves to Redis, keyed by host: tier, circuit state, interval. Every
worker then shares one view of each site. Browser rendering becomes a separate worker pool fed by
a queue, because it costs about 50x an HTTP fetch, and it only receives URLs whose host is on the
browser tier. Blocked-host rates are a dashboard metric: a rising rate means the crawl is too
aggressive, not that the evasion needs to improve.
