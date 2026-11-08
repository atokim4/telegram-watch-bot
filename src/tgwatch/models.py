"""Domain types shared by the fetcher, parsers, store and engine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

MIN_INTERVAL_SECONDS = 5 * 60
"""Polite lower bound for how often one watch may hit its source."""


class WatchKind(StrEnum):
    RSS = "rss"
    HTML = "html"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class ParseSpec:
    """How to turn a fetched document into items.

    rss:  ``items`` is unused; ``fields`` may name extra entry keys to keep (e.g. ``author``).
    html: ``items`` is a CSS selector matching one element per item; ``fields`` maps names to
          CSS selectors relative to that element (``title``, ``link`` and any extra like ``price``).
    json: ``items`` is a path to the list of items (``data.children[*].data``); ``fields`` maps
          names to paths relative to one item.
    """

    items: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KeywordRule:
    """Include terms: at least one must appear (empty = everything). Exclude: none may appear."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(slots=True)
class Watch:
    id: int
    chat_id: int
    name: str
    url: str
    kind: WatchKind
    spec: ParseSpec = field(default_factory=ParseSpec)
    keywords: KeywordRule = field(default_factory=KeywordRule)
    criteria: str = ""
    """Optional natural-language relevance criteria for the Claude filter."""
    interval_seconds: int = MIN_INTERVAL_SECONDS
    paused: bool = False
    baseline_done: bool = False
    """False until the first good check; that check records what exists without alerting."""
    next_due_at: datetime | None = None
    last_checked_at: datetime | None = None
    error_count: int = 0
    last_error: str = ""
    etag: str = ""
    last_modified: str = ""


@dataclass(frozen=True, slots=True)
class Item:
    """One entry found in a source. ``uid`` is the most stable identifier the source offers."""

    uid: str
    title: str
    link: str = ""
    summary: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Fixed-length hash used for the seen-items table."""
        return hashlib.sha256(self.uid.encode("utf-8")).hexdigest()[:32]

    def searchable_text(self) -> str:
        parts = [self.title, self.summary, *self.fields.values()]
        return "\n".join(p for p in parts if p)
