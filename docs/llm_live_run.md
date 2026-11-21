# LLM engine: live runs

Produced by `scripts/llm_live_check.py` against the real APIs. Models: Gemini `gemini-3.5-flash` (thinking level `minimal`), Groq `openai/gpt-oss-120b`; DeepSeek had no key and was skipped by the chain. Each scenario keeps the time it was last run. Nothing was stored.

Columns: **attempts** lists each provider call in order (`provider:outcome[input tokens]`); **tokens** is sent/original after chunking; **dropped** lists fields the grounding gate nulled because the value was not in the page text.

## A. Full chain on real pages

Run 2026-11-21 22:31 UTC. Settings: `chain=gemini,groq,deepseek`

| Page | Served by | Attempts | Tokens | Dropped | Time |
|---|---|---|---|---|---|
| startup: https://www.greptile.com/ | gemini (gemini-3.5-flash) | gemini:ok[2004t] | 2004/2004 | none | 6.8 s |
| startup: https://www.trychroma.com/ | gemini (gemini-3.5-flash) | gemini:ok[2540t] | 2540/2540 | none | 2.9 s |
| product: https://www.xano.com/ | gemini (gemini-3.5-flash) | gemini:ok[1447t] | 1447/1447 | none | 1.5 s |
| job: https://www.arbeitnow.nl/jobs/companies/luke-recruitment/senior-social-ai-marketeer-tilburg-68538 | gemini (gemini-3.5-flash) | gemini:ok[3463t] | 3463/3463 | none | 3.1 s |
| news: https://techcrunch.com/2026/11/21/all-the-ai-agents-that-can-live-in-your-text-messages/ | gemini (gemini-3.5-flash) | gemini:ok[4877t] | 4877/4877 | none | 23.1 s |

<details><summary>Extracted data</summary>

```json
{
 "https://www.greptile.com/": {
  "entityName": "Greptile",
  "description": "Greptile is an enterprise-grade AI code review platform that automates pull request analysis, detects bugs, and enforces coding standards.",
  "website": "https://www.greptile.com"
 },
 "https://www.trychroma.com/": {
  "entityName": "Chroma",
  "description": "Chroma provides fast, serverless, and scalable open-source search infrastructure for AI supporting vector, full-text, regex, and metadata search.",
  "website": "https://www.trychroma.com/"
 },
 "https://www.xano.com/": {
  "productName": "Xano",
  "startupName": "Xano",
  "description": "One backend platform to build, run and govern AI-built software: build with AI, in XanoScript, or in the Xano editor, then test, deploy, and observe it all.",
  "website": "https://www.xano.com/",
  "pricingModel": "FREEMIUM",
  "categories": [
   "Backend Platform",
   "AI Software Governance",
   "Developer Tools",
   "APIs",
   "MCP Servers"
  ]
 },
 "https://www.arbeitnow.nl/jobs/companies/luke-recruitment/senior-social-ai-marketeer-tilburg-68538": {
  "title": "Senior Social & AI Marketeer",
  "company": "Luke Recruitment",
  "location": "Tilburg",
  "date_text": "1 day",
  "role_family": "Marketing"
 },
 "https://techcrunch.com/2026/11/21/all-the-ai-agents-that-can-live-in-your-text-messages/": {
  "title": "Here are the top AI agents that can live in your text messages",
  "author": "Lauren Forristal",
  "date_text": "2026-11-21T17:00:00+00:00",
  "summary": "A growing category of AI agents can now operate directly within text messaging platforms such as iMessage, SMS, WhatsApp, and Telegram without requiring separate applications. These agents assist users with everyday tasks including calendar organization, travel planning, family life management, and professional duties. The article details a wide range of notable conversational assistants, highlighting their features, pricing, platform availability, and funding status.",
  "companies": [
   "TechCrunch",
   "Ask Proxy",
   "Caddy",
   "Comma",
   "Fambot",
   "Folk",
   "Haven",
   "Instinct",
   "Iris",
   "Martin",
   "Maverick",
   "Miso",
   "Ohai",
   "Orbits",
   "Andreessen Horowitz",
   "N49P",
   "Garage Capital",
   "Pally",
   "Poke",
   "The Interaction Company of California",
   "Apple",
   "Cognition",
   "Rene",
   "Skye",
   "Signull Labs",
   "Stanley",
   "Szn",
   "Tab",
   "SV Angel",
   "Valar Ventures",
   "American Spirit",
   "Tomo",
   "Town",
   "Forerunner",
   "Underdog",
   "Wajo",
   "Microsoft",
   "Nvidia",
   "Amazon",
   "Google",
   "Anthropic"
  ]
 }
}
```

</details>

Orchestrator counters: `{"gemini.calls": 5, "gemini.ok": 5}`

## B. Provider failure → fall-through

Run 2026-11-21 22:31 UTC. Settings: `gemini_model=gemini-0-does-not-exist`

| Page | Served by | Attempts | Tokens | Dropped | Time |
|---|---|---|---|---|---|
| startup: https://www.greptile.com/ | groq (openai/gpt-oss-120b) | gemini:fatal[2004t] → groq:ok[2004t] | 2004/2004 | none | 1.6 s |
| product: https://www.xano.com/ | groq (openai/gpt-oss-120b) | groq:ok[1447t] | 1447/1447 | none | 1.0 s |

<details><summary>Extracted data</summary>

```json
{
 "https://www.greptile.com/": {
  "entityName": "Greptile",
  "description": "An AI-powered code review platform that automates analysis, detects bugs, and enforces best practices.",
  "website": "https://www.greptile.com"
 },
 "https://www.xano.com/": {
  "productName": "Xano",
  "startupName": "Xano",
  "description": "One backend platform to build, run and govern AI-built software",
  "website": "https://www.xano.com/",
  "pricingModel": "FREEMIUM",
  "categories": [
   "Backend Platform",
   "AI",
   "API Development",
   "Low-code"
  ]
 }
}
```

</details>

Orchestrator counters: `{"gemini.calls": 1, "gemini.fallthrough": 1, "groq.calls": 2, "groq.ok": 2}`

## C. Oversized pages: chunker, then a real 413

Run 2026-11-21 22:58 UTC. Settings: `(1) default budgets; (2) chain=groq, groq_max_input_tokens=20000 (real limit 8000), limiter off`

| Page | Served by | Attempts | Tokens | Dropped | Time |
|---|---|---|---|---|---|
| news: https://arxiv.org/html/2402.06196 | groq (openai/gpt-oss-120b) | gemini:quota[26402t] → groq:ok[4031t] | 4031/54500 truncated | none | 2.0 s |
| news: https://arxiv.org/html/2402.06196 | groq (openai/gpt-oss-120b) | groq:413[17586t] → groq:429[8031t] → groq:ok[8031t] | 8031/54500 truncated | author | 32.8 s |

<details><summary>Extracted data</summary>

```json
{
 "https://arxiv.org/html/2402.06196": {
  "title": "Large Language Models: A Survey",
  "date_text": "23 Mar 2025",
  "summary": "The paper surveys recent advances in large language models (LLMs), reviewing prominent families such as GPT, LLaMA, and PaLM, their characteristics, datasets, and evaluation metrics. It discusses techniques for building and augmenting LLMs, compares performance on benchmarks, and outlines open challenges and future research directions. The authors aim to provide a valuable resource for students, researchers, and developers working with LLM-powered AI systems.",
  "companies": [
   "Amazon Inc",
   "Ultimate.ai",
   "You.com",
   "Google Inc",
   "Microsoft Research",
   "Meta",
   "OpenAI"
  ]
 }
}
```

</details>

Orchestrator counters: `{"1.gemini.calls": 1, "1.gemini.429": 1, "1.gemini.quota": 1, "1.gemini.fallthrough": 1, "1.groq.calls": 1, "1.groq.ok": 1, "2.groq.calls": 3, "2.groq.413": 1, "2.groq.429": 1, "2.groq.ok": 1}`

## D. Burst over Groq's 8k tokens/min → real 429s

Run 2026-11-21 22:31 UTC. Settings: `chain=groq,gemini; client limiter off for Groq; llm_max_429_retries=1; 6 concurrent requests`

| Page | Served by | Attempts | Tokens | Dropped | Time |
|---|---|---|---|---|---|
| startup: https://www.greptile.com/ | groq (openai/gpt-oss-120b) | groq:ok[2004t] | 2004/2004 | none | 0.8 s |
| startup: https://www.trychroma.com/ | groq (openai/gpt-oss-120b) | groq:429[2540t] → groq:ok[2540t] | 2540/2540 | none | 26.8 s |
| startup: https://www.xano.com/ | gemini (gemini-3.5-flash) | groq:429[1447t] → groq:429[1447t] → gemini:ok[1447t] | 1447/1447 | none | 28.4 s |
| startup: https://modal.com/ | groq (openai/gpt-oss-120b) | groq:429[1719t] → groq:ok[1719t] | 1719/1719 | none | 26.8 s |
| startup: https://www.reducto.ai/ | groq (openai/gpt-oss-120b) | groq:ok[1438t] | 1438/1438 | none | 1.0 s |
| startup: https://synthflow.ai/ | gemini (gemini-3.5-flash) | groq:429[4105t] → groq:429[4105t] → gemini:ok[4105t] | 4105/4105 | none | 29.6 s |

<details><summary>Extracted data</summary>

```json
{
 "https://www.greptile.com/": {
  "entityName": "Greptile",
  "description": "Enterprise‑grade AI code review platform that automates code analysis, detects bugs, and enforces best practices.",
  "website": "https://www.greptile.com"
 },
 "https://www.trychroma.com/": {
  "entityName": "Chroma",
  "description": "Open-source search infrastructure for AI",
  "website": "https://www.trychroma.com/"
 },
 "https://www.xano.com/": {
  "entityName": "Xano",
  "description": "Xano is a backend platform to build, run, and govern AI-built software, including apps, APIs, and MCP servers.",
  "website": "https://www.xano.com/"
 },
 "https://modal.com/": {
  "entityName": "Modal",
  "description": "Cloud infrastructure for teams that develop, train, and serve AI applications at scale.",
  "website": "https://modal.com/"
 },
 "https://www.reducto.ai/": {
  "entityName": "Reducto",
  "description": "Reducto is the agentic document platform for leading AI teams—a comprehensive toolkit for every document task, built for enterprise performance at scale.",
  "website": "https://reducto.ai",
  "industries": [
   "BusinessApplication"
  ]
 },
 "https://synthflow.ai/": {
  "entityName": "Synthflow",
  "description": "Synthflow provides enterprise AI voice agents that answer, make, and handle phone calls for contact centers, BPOs, and multi-location brands within their existing phone systems and CRMs.",
  "website": "https://synthflow.ai/",
  "industries": [
   "Artificial Intelligence",
   "Conversational AI",
   "Enterprise Software",
   "Telecommunications"
  ]
 }
}
```

</details>

Orchestrator counters: `{"groq.calls": 10, "groq.429": 6, "groq.ok": 4, "groq.fallthrough": 2, "gemini.calls": 2, "gemini.ok": 2}`

## E. Provider out of daily quota → immediate fall-through

Run 2026-11-21 22:57 UTC. Settings: `chain=gemini,groq,deepseek; Gemini free-tier daily cap exhausted; llm_max_retry_wait_s=60.0`

| Page | Served by | Attempts | Tokens | Dropped | Time |
|---|---|---|---|---|---|
| startup: https://www.greptile.com/ | groq (openai/gpt-oss-120b) | gemini:quota[2004t] → groq:429[2004t] → groq:ok[2004t] | 2004/2004 | none | 18.5 s |
| product: https://www.xano.com/ | groq (openai/gpt-oss-120b) | gemini:cooldown → groq:429[1447t] → groq:ok[1447t] | 1447/1447 | none | 30.0 s |

<details><summary>Extracted data</summary>

```json
{
 "https://www.greptile.com/": {
  "entityName": "Greptile",
  "description": "Enterprise‑grade AI code review platform that automates code analysis, detects bugs, and enforces best practices.",
  "website": "https://www.greptile.com"
 },
 "https://www.xano.com/": {
  "productName": "Xano",
  "startupName": "Xano",
  "description": "One backend platform to build, run and govern AI-built software.",
  "website": "https://www.xano.com/",
  "pricingModel": "FREEMIUM",
  "categories": [
   "Backend Platform",
   "AI Software",
   "API Development"
  ]
 }
}
```

</details>

Orchestrator counters: `{"gemini.calls": 1, "gemini.429": 1, "gemini.quota": 1, "gemini.fallthrough": 1, "groq.calls": 4, "groq.429": 2, "groq.ok": 2}`
