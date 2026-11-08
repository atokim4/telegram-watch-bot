"""Polls due watches on a timer and hands new matching items to a notifier."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from tgwatch.engine import Alert, CheckResult, WatchEngine
from tgwatch.errors import WatchError
from tgwatch.models import Watch
from tgwatch.store import Store, utcnow

log = logging.getLogger(__name__)

MAX_BACKOFF = timedelta(hours=6)
NOTIFY_AFTER_ERRORS = 3
"""Tell the owner once, on the third failure in a row, instead of on every flaky poll."""


class Notifier(Protocol):
    async def send_alert(self, watch: Watch, alert: Alert) -> None: ...

    async def send_watch_failing(self, watch: Watch, error: str) -> None: ...


class Scheduler:
    def __init__(
        self,
        store: Store,
        engine: WatchEngine,
        notifier: Notifier,
        *,
        tick_seconds: float = 30.0,
        concurrency: int = 4,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._engine = engine
        self._notifier = notifier
        self._tick = tick_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self._clock = clock

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("scheduler started, tick %.0fs", self._tick)
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("scheduler tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._tick)
        log.info("scheduler stopped")

    async def run_once(self) -> int:
        """Check every due watch once. Returns how many were checked."""
        due = await self._store.due_watches(self._clock())
        await asyncio.gather(*(self._guarded(watch) for watch in due))
        return len(due)

    async def _guarded(self, watch: Watch) -> None:
        async with self._semaphore:
            try:
                await self.process(watch)
            except Exception:
                # e.g. the watch was deleted mid-check; one bad watch must not stop the tick.
                log.exception("watch %s: bookkeeping failed", watch.id)

    async def process(self, watch: Watch) -> CheckResult | None:
        started = self._clock()
        try:
            result = await self._engine.check(watch)
        except WatchError as exc:
            await self._failed(watch, started, str(exc))
            return None
        except Exception:
            log.exception("watch %s crashed", watch.id)
            await self._failed(watch, started, "internal error while checking this watch")
            return None

        await self._store.mark_seen(watch.id, result.silent_keys, now=started)
        delivered_all = await self._deliver(watch, result)
        # Keep the old validators when delivery failed, so a 304 cannot hide the unsent items.
        await self._store.record_success(
            watch.id,
            checked_at=started,
            next_due_at=started + _jittered(watch.interval_seconds),
            etag=result.etag if delivered_all else None,
            last_modified=result.last_modified if delivered_all else None,
        )
        if result.baseline:
            log.info("watch %s baseline: %d items", watch.id, result.items_found)
        return result

    async def _deliver(self, watch: Watch, result: CheckResult) -> bool:
        for alert in result.alerts:
            try:
                await self._notifier.send_alert(watch, alert)
            except Exception:
                # Unsent items stay unseen and go out on the next poll.
                log.exception("could not deliver alert for watch %s", watch.id)
                return False
            await self._store.mark_seen(watch.id, [alert.item.key])
        return True

    async def _failed(self, watch: Watch, started: datetime, error: str) -> None:
        exponent = min(watch.error_count, 10)
        backoff = min(timedelta(seconds=watch.interval_seconds * 2**exponent), MAX_BACKOFF)
        count = await self._store.record_failure(
            watch.id, checked_at=started, next_due_at=started + backoff, error=error
        )
        log.warning("watch %s failed (%d in a row): %s", watch.id, count, error)
        if count == NOTIFY_AFTER_ERRORS:
            try:
                await self._notifier.send_watch_failing(watch, error)
            except Exception:
                log.exception("could not tell chat %s about failing watch", watch.chat_id)


def _jittered(interval_seconds: int) -> timedelta:
    # Up to +10% so watches created together drift apart; not security relevant.
    return timedelta(seconds=interval_seconds * (1 + random.random() / 10))  # noqa: S311
