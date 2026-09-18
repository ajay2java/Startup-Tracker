"""Product Hunt — newest launches.

Uses the Product Hunt API v2 (GraphQL). A free developer token is required
(``PRODUCT_HUNT_TOKEN`` in ``.env``); without it this source is skipped and a
non-blocking warning is surfaced in the UI. Each post node is written
verbatim into ``raw_records``.
"""
from __future__ import annotations

import logging

import httpx

from ..config import HTTP_TIMEOUT, PRODUCT_HUNT_TOKEN
from .base import FetchResult, RawItem

log = logging.getLogger(__name__)

API_URL = "https://api.producthunt.com/v2/api/graphql"
MAX_RESULTS = 50

QUERY = """
query RecentPosts($first: Int!) {
  posts(order: NEWEST, first: $first) {
    edges {
      node {
        id
        name
        tagline
        url
        website
        createdAt
        topics(first: 5) { edges { node { name } } }
      }
    }
  }
}
"""


async def fetch(client: httpx.AsyncClient) -> FetchResult:
    if not PRODUCT_HUNT_TOKEN:
        return FetchResult.failed(
            "product_hunt",
            "Product Hunt skipped — set PRODUCT_HUNT_TOKEN in .env to enable it",
        )

    headers = {
        "Authorization": f"Bearer {PRODUCT_HUNT_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": QUERY, "variables": {"first": MAX_RESULTS}}
    try:
        resp = await client.post(
            API_URL, json=payload, headers=headers, timeout=HTTP_TIMEOUT
        )
        if resp.status_code in (401, 403):
            return FetchResult.failed(
                "product_hunt", "Product Hunt token rejected (401/403) — check .env"
            )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPStatusError as exc:
        return FetchResult.failed(
            "product_hunt", f"Product Hunt returned HTTP {exc.response.status_code}"
        )
    except (httpx.HTTPError, ValueError) as exc:
        return FetchResult.failed("product_hunt", f"Product Hunt fetch failed: {exc}")

    if body.get("errors"):
        msg = body["errors"][0].get("message", "unknown error")
        return FetchResult.failed("product_hunt", f"Product Hunt API error: {msg}")

    edges = (((body.get("data") or {}).get("posts") or {}).get("edges")) or []
    raw_items: list[RawItem] = []
    seen: set[str] = set()
    for edge in edges:
        node = edge.get("node") or {}
        node_id = node.get("id")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        raw_items.append(RawItem(source_id=str(node_id), payload=node))

    log.info("Product Hunt: %d raw items", len(raw_items))
    return FetchResult(source="product_hunt", items=raw_items)
