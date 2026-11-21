"""Live GitHub metrics (stars) for paper repositories.

With GITHUB_TOKEN: GraphQL, 100 repositories per request via aliases
(1,000 papers = 10 requests; 500k papers = 5k requests ≈ one hour of the
5,000-points/h budget, or minutes with a small token pool).
Without a token: REST, limited to the 60 requests/hour anonymous quota, so we
spend only what is left and keep the source-reported count for the rest.
Every value records where it came from and when it was checked.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from ...core.http import FetchError, HttpFetcher
from ...core.logging import get_logger

log = get_logger(__name__)
GRAPHQL = "https://api.github.com/graphql"
REST = "https://api.github.com/repos/{slug}"


@dataclass(slots=True)
class RepoMetrics:
    slug: str                 # canonical owner/name (after renames)
    stars: int | None
    forks: int | None = None
    archived: bool | None = None
    pushed_at: str | None = None
    exists: bool = True
    checked_at: datetime | None = None
    source: str = "github_api"


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class GitHubMetrics:
    def __init__(self, http: HttpFetcher, token: str | None, *, batch_size: int = 100):
        self.http = http
        self.token = token
        self.batch_size = batch_size

    @property
    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def fetch(self, slugs: list[str]) -> dict[str, RepoMetrics]:
        slugs = list(dict.fromkeys(s.lower() for s in slugs))
        if not slugs:
            return {}
        if self.token:
            return await self._graphql_all(slugs)
        return await self._rest_within_quota(slugs)

    # ------------------------------------------------------------------ GraphQL
    async def _graphql_all(self, slugs: list[str]) -> dict[str, RepoMetrics]:
        batches = [slugs[i : i + self.batch_size] for i in range(0, len(slugs), self.batch_size)]
        results = await asyncio.gather(*(self._graphql_batch(b) for b in batches))
        out: dict[str, RepoMetrics] = {}
        for r in results:
            out.update(r)
        return out

    async def _graphql_batch(self, slugs: list[str]) -> dict[str, RepoMetrics]:
        parts = []
        for i, slug in enumerate(slugs):
            owner, name = slug.split("/", 1)
            parts.append(
                f'r{i}: repository(owner: "{_q(owner)}", name: "{_q(name)}") '
                "{ nameWithOwner stargazerCount forkCount isArchived pushedAt }"
            )
        query = "query {\n" + "\n".join(parts) + "\n rateLimit { remaining resetAt } }"
        try:
            resp = await self.http.request("POST", GRAPHQL, json={"query": query}, headers=self._headers)
        except FetchError as exc:
            log.error("github.graphql_failed", error=str(exc), batch=len(slugs))
            return {}
        payload = resp.json()
        data = payload.get("data") or {}
        now = datetime.now(timezone.utc)
        out: dict[str, RepoMetrics] = {}
        for i, slug in enumerate(slugs):
            node = data.get(f"r{i}")
            if node is None:  # deleted / private / renamed beyond redirect
                out[slug] = RepoMetrics(slug, None, exists=False, checked_at=now)
                continue
            out[slug] = RepoMetrics(
                slug=node["nameWithOwner"].lower(),
                stars=node["stargazerCount"],
                forks=node["forkCount"],
                archived=node["isArchived"],
                pushed_at=node["pushedAt"],
                checked_at=now,
            )
        rl = data.get("rateLimit") or {}
        log.info("github.graphql_batch", repos=len(slugs), found=sum(m.exists for m in out.values()),
                 remaining=rl.get("remaining"))
        return out

    # ------------------------------------------------------------------ REST (anonymous)
    async def remaining_quota(self) -> int:
        try:
            data = await self.http.get_json("https://api.github.com/rate_limit", headers=self._headers)
            return int(data["resources"]["core"]["remaining"])
        except (FetchError, KeyError, ValueError):
            return 0

    async def _rest_within_quota(self, slugs: list[str]) -> dict[str, RepoMetrics]:
        quota = await self.remaining_quota()
        budget = max(0, quota - 2)
        if budget < len(slugs):
            log.warning("github.no_token", needed=len(slugs), anonymous_quota_left=quota,
                        hint="set GITHUB_TOKEN to refresh all repositories live")
        out: dict[str, RepoMetrics] = {}

        async def one(slug: str) -> None:
            try:
                resp = await self.http.get(REST.format(slug=slug), headers=self._headers,
                                           ok_statuses=(200, 404, 451), max_retries=1)
            except FetchError as exc:
                log.debug("github.rest_failed", slug=slug, error=str(exc))
                return
            now = datetime.now(timezone.utc)
            if resp.status_code != 200:
                out[slug] = RepoMetrics(slug, None, exists=False, checked_at=now)
                return
            d = resp.json()
            out[slug] = RepoMetrics(d["full_name"].lower(), d["stargazers_count"], d.get("forks_count"),
                                    d.get("archived"), d.get("pushed_at"), checked_at=now)

        await asyncio.gather(*(one(s) for s in slugs[:budget]))
        return out
