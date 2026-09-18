"""Hacker News — Show HN and Launch HN posts.

"Show HN" is where founders post things they have built; "Launch HN" is the
YC-sanctioned launch post format. Both are strong early signals. We use the
free Algolia HN Search API (no key required) and write each hit verbatim —
title parsing into a candidate name is a gate/derive concern, not a fetch
concern.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from ..config import HTTP_TIMEOUT
from .base import FetchResult, RawItem

log = logging.getLogger(__name__)

API_URL = "https://hn.algolia.com/api/v1/search_by_date"
LOOKBACK_DAYS = 30
MAX_RESULTS = 100


async def fetch(client: httpx.AsyncClient) -> FetchResult:
    cutoff = int(
        (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp()
    )
    raw_items: list[RawItem] = []
    seen: set[str] = set()

    try:
        for tag in ("show_hn", "story"):
            params = {
                "tags": tag,
                "hitsPerPage": MAX_RESULTS,
                "numericFilters": f"created_at_i>{cutoff}",
            }
            if tag == "story":
                params["query"] = "Launch HN"
            resp = await client.get(API_URL, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                title = hit.get("title") or ""
                if tag == "story" and "launch hn" not in title.lower():
                    continue
                obj_id = str(hit.get("objectID"))
                if not obj_id or obj_id in seen:
                    continue
                seen.add(obj_id)
                raw_items.append(RawItem(source_id=obj_id, payload=hit))
    except httpx.HTTPStatusError as exc:
        return FetchResult.failed(
            "hn", f"Hacker News returned HTTP {exc.response.status_code}"
        )
    except (httpx.HTTPError, ValueError) as exc:
        return FetchResult.failed("hn", f"Hacker News fetch failed: {exc}")

    log.info("Hacker News: %d raw items", len(raw_items))
    return FetchResult(source="hn", items=raw_items)
