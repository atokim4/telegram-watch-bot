"""One check of one watch: fetch, parse, find new items, apply keyword and AI filters."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from tgwatch import filters
from tgwatch.ai_filter import RelevanceFilter
from tgwatch.fetch import Fetcher, FetchResult
from tgwatch.models import Item, Watch, WatchKind
from tgwatch.parsers import parse
from tgwatch.store import Store

log = logging.getLogger(__name__)

ACCEPT = {
    WatchKind.RSS: "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5",
    WatchKind.HTML: "text/html, application/xhtml+xml;q=0.9, */*;q=0.5",
    WatchKind.JSON: "application/json, */*;q=0.5",
}


class AiStatus(StrEnum):
    OFF = "off"
    """The watch has no criteria, or no API key is configured."""
    PASSED = "passed"
    UNCHECKED = "unchecked"
    """The filter could not decide (API error) or the per-check limit was reached; sent anyway."""


@dataclass(frozen=True, slots=True)
class Alert:
    item: Item
    ai_status: AiStatus = AiStatus.OFF
    ai_reason: str = ""


@dataclass(slots=True)
class CheckResult:
    watch_id: int
    items_found: int = 0
    baseline: bool = False
    not_modified: bool = False
    alerts: list[Alert] = field(default_factory=list)
    silent_keys: list[str] = field(default_factory=list)
    """New items that will not be sent (filtered out, or baseline): mark seen right away."""
    rejected: list[tuple[Item, str]] = field(default_factory=list)
    """Items the AI filter turned down, with its reason (used by the dry run)."""
    etag: str = ""
    last_modified: str = ""
    warning: str = ""


class WatchEngine:
    def __init__(
        self,
        store: Store,
        fetcher: Fetcher,
        relevance: RelevanceFilter | None = None,
        *,
        ai_max_items_per_check: int = 20,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._relevance = relevance
        self._ai_max_items = ai_max_items_per_check

    @property
    def ai_enabled(self) -> bool:
        return self._relevance is not None

    async def check(self, watch: Watch) -> CheckResult:
        """Find items not seen before. Nothing is written; the scheduler marks items seen."""
        fetched = await self._fetcher.fetch(
            watch.url,
            accept=ACCEPT[watch.kind],
            etag=watch.etag if watch.baseline_done else "",
            last_modified=watch.last_modified if watch.baseline_done else "",
        )
        result = CheckResult(
            watch_id=watch.id, etag=fetched.etag, last_modified=fetched.last_modified
        )
        if fetched.not_modified:
            result.not_modified = True
            return result

        items = self._parse(watch, fetched, result)
        unseen = await self._store.unseen_keys(watch.id, [item.key for item in items])
        new_items = [item for item in items if item.key in unseen]

        if not watch.baseline_done:
            result.baseline = True
            result.silent_keys = [item.key for item in new_items]
            return result

        matched: list[Item] = []
        for item in new_items:
            if filters.matches(watch.keywords, item):
                matched.append(item)
            else:
                result.silent_keys.append(item.key)
        await self._apply_relevance(watch, matched, result)
        return result

    async def preview(self, watch: Watch, *, limit: int = 5) -> CheckResult:
        """Dry run for /test: what would alert if every current item were new."""
        fetched = await self._fetcher.fetch(watch.url, accept=ACCEPT[watch.kind])
        result = CheckResult(watch_id=watch.id)
        items = self._parse(watch, fetched, result)
        matched = [item for item in items if filters.matches(watch.keywords, item)][:limit]
        await self._apply_relevance(watch, matched, result)
        return result

    def _parse(self, watch: Watch, fetched: FetchResult, result: CheckResult) -> list[Item]:
        items = parse(watch.kind, fetched.content, fetched.url, watch.spec)
        result.items_found = len(items)
        if not items:
            result.warning = (
                "the selector matched nothing; the page layout may have changed"
                if watch.kind is not WatchKind.RSS
                else "the feed has no entries right now"
            )
        return items

    async def _apply_relevance(
        self, watch: Watch, matched: list[Item], result: CheckResult
    ) -> None:
        if not watch.criteria or self._relevance is None:
            result.alerts.extend(Alert(item=item) for item in matched)
            return
        to_judge = matched[: self._ai_max_items]
        verdicts = await asyncio.gather(
            *(self._relevance.judge(item, watch.criteria) for item in to_judge)
        )
        for item, verdict in zip(to_judge, verdicts, strict=True):
            if verdict is None:
                result.alerts.append(
                    Alert(item=item, ai_status=AiStatus.UNCHECKED, ai_reason="AI check unavailable")
                )
            elif verdict.relevant:
                result.alerts.append(
                    Alert(item=item, ai_status=AiStatus.PASSED, ai_reason=verdict.reason)
                )
            else:
                result.silent_keys.append(item.key)
                result.rejected.append((item, verdict.reason))
        result.alerts.extend(
            Alert(
                item=item,
                ai_status=AiStatus.UNCHECKED,
                ai_reason="AI check skipped: too many new items at once",
            )
            for item in matched[self._ai_max_items :]
        )
