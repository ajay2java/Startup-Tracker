"""Common types shared by every source fetcher.

Each fetcher is an ``async`` callable that returns a ``FetchResult``: a list
of ``RawItem`` (a source-native id plus its verbatim payload dict, headed
straight into ``raw_records`` with no cleaning), or a failure marker with a
human-readable reason the UI surfaces as a non-blocking warning. All
filtering/normalization (US-only, structural rejects, name cleanup, sector
guessing) happens later in the gate and derive stages — a fetcher's only job
is "get the data, verbatim."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawItem:
    source_id: str
    payload: dict[str, Any]


@dataclass
class FetchResult:
    source: str
    items: list[RawItem] = field(default_factory=list)
    ok: bool = True
    warning: str | None = None

    @classmethod
    def failed(cls, source: str, reason: str) -> "FetchResult":
        return cls(source=source, items=[], ok=False, warning=reason)
