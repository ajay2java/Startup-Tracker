"""GitHub — recently created, fast-growing repositories.

A young repo that is already accumulating stars often belongs to a company
in stealth or just-launched. We query the Search API for repositories
created within a recent window that have cleared a small star threshold.
Every hit is written verbatim — whether the owner is an "Organization" (vs.
a personal account) is a gate concern (layer 1: structural), not a fetch
concern.

A personal access token (``GITHUB_TOKEN`` in ``.env``) is recommended — it
raises the rate limit from 10 to 30 search requests/minute. Without one the
fetch still runs but is more likely to be throttled.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from ..config import GITHUB_TOKEN, HTTP_TIMEOUT
from .base import FetchResult, RawItem

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/search/repositories"
LOOKBACK_DAYS = 120
MIN_STARS = 40
MAX_RESULTS = 60


async def fetch(client: httpx.AsyncClient) -> FetchResult:
    since = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    params = {
        "q": f"created:>{since} stars:>{MIN_STARS}",
        "sort": "stars",
        "order": "desc",
        "per_page": min(MAX_RESULTS, 100),
    }
    try:
        resp = await client.get(
            API_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT
        )
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            hint = "" if GITHUB_TOKEN else " (set GITHUB_TOKEN in .env to raise the limit)"
            return FetchResult.failed("github", f"GitHub rate limit hit{hint}")
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except httpx.HTTPStatusError as exc:
        return FetchResult.failed(
            "github", f"GitHub returned HTTP {exc.response.status_code}"
        )
    except (httpx.HTTPError, ValueError) as exc:
        return FetchResult.failed("github", f"GitHub fetch failed: {exc}")

    raw_items = [
        RawItem(source_id=str(repo["id"]), payload=repo)
        for repo in items
        if repo.get("id") is not None
    ]
    log.info("GitHub: %d raw items", len(raw_items))
    return FetchResult(source="github", items=raw_items)
