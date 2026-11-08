"""End to end: a local site changes between polls; the scheduler alerts only on new matches."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from conftest import Site, fixture_bytes

from tgwatch.ai_filter import Verdict
from tgwatch.engine import AiStatus, Alert, WatchEngine
from tgwatch.fetch import Fetcher
from tgwatch.filters import parse_keywords
from tgwatch.models import Item, ParseSpec, Watch, WatchKind
from tgwatch.scheduler import NOTIFY_AFTER_ERRORS, Scheduler
from tgwatch.store import NewWatch, Store

RSS_ITEM = """<item><title>{title}</title><link>https://jobs.example.com/{uid}</link>
<guid>{uid}</guid><description>{desc}</description></item>"""


def rss(*items: tuple[str, str, str]) -> str:
    body = "".join(RSS_ITEM.format(uid=u, title=t, desc=d) for u, t, d in items)
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'
    )


@dataclass
class FakeNotifier:
    alerts: list[tuple[int, Alert]] = field(default_factory=list)
    failing: list[tuple[int, str]] = field(default_factory=list)
    broken: bool = False

    async def send_alert(self, watch: Watch, alert: Alert) -> None:
        if self.broken:
            raise ConnectionError("telegram is down")
        self.alerts.append((watch.id, alert))

    async def send_watch_failing(self, watch: Watch, error: str) -> None:
        self.failing.append((watch.id, error))

    def titles(self) -> list[str]:
        return [alert.item.title for _, alert in self.alerts]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


@dataclass
class FakeRelevance:
    """Relevant when the summary says "remote"; None (undecided) when it says "???"."""

    calls: list[str] = field(default_factory=list)

    async def judge(self, item: Item, criteria: str) -> Verdict | None:
        self.calls.append(item.title)
        if "???" in item.summary:
            return None
        relevant = "remote" in item.summary.lower()
        return Verdict(relevant=relevant, reason="remote role" if relevant else "on-site only")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def make_scheduler(
    store: Store,
    fetcher: Fetcher,
    notifier: FakeNotifier,
    clock: Clock,
    relevance: FakeRelevance | None = None,
) -> Scheduler:
    return Scheduler(store, WatchEngine(store, fetcher, relevance), notifier, clock=clock)


async def add(
    store: Store, site: Site, clock: Clock, path: str = "/jobs.rss", **kw: object
) -> Watch:
    fields: dict[str, object] = {
        "chat_id": 42,
        "name": "jobs",
        "url": site.url(path),
        "kind": WatchKind.RSS,
        "interval_seconds": 300,
    }
    fields.update(kw)
    return await store.add_watch(NewWatch(**fields), now=clock.now)  # type: ignore[arg-type]


async def test_baseline_then_only_new_matching_items(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    site.set(
        "/jobs.rss", rss(("1", "Python bot", ""), ("2", "Java app", "")), "application/rss+xml"
    )
    await add(store, site, clock, keywords=parse_keywords("python, bot, -crypto"))
    scheduler = make_scheduler(store, fetcher, notifier, clock)

    assert await scheduler.run_once() == 1
    assert notifier.alerts == []  # first poll records what exists, no flood

    assert await scheduler.run_once() == 0  # not due yet
    clock.advance(6)
    site.set(
        "/jobs.rss",
        rss(
            ("3", "Telegram bot in Python", "remote"),
            ("4", "Crypto trading bot", ""),
            ("5", "Logo design", ""),
            ("1", "Python bot", ""),
            ("2", "Java app", ""),
        ),
        "application/rss+xml",
    )
    assert await scheduler.run_once() == 1
    assert notifier.titles() == ["Telegram bot in Python"]

    clock.advance(6)
    await scheduler.run_once()
    assert notifier.titles() == ["Telegram bot in Python"]  # nothing new, nothing sent


async def test_etag_304_skips_parsing(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    site.set("/jobs.rss", rss(("1", "a", "")), "application/rss+xml", etag='"v1"')
    watch = await add(store, site, clock)
    scheduler = make_scheduler(store, fetcher, notifier, clock)
    await scheduler.run_once()
    clock.advance(6)
    stored = await store.get_watch(watch.id)
    assert stored is not None
    assert stored.etag == '"v1"'
    result = await scheduler.process(stored)
    assert result is not None
    assert result.not_modified


async def test_failed_delivery_is_retried_next_poll(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    site.set("/jobs.rss", rss(("1", "old", "")), "application/rss+xml", etag='"v1"')
    await add(store, site, clock)
    scheduler = make_scheduler(store, fetcher, notifier, clock)
    await scheduler.run_once()

    site.set(
        "/jobs.rss", rss(("2", "new one", ""), ("1", "old", "")), "application/rss+xml", etag='"v2"'
    )
    notifier.broken = True
    clock.advance(6)
    await scheduler.run_once()
    assert notifier.alerts == []

    notifier.broken = False
    clock.advance(6)
    await scheduler.run_once()  # old validator kept, so no 304 hides the unsent item
    assert notifier.titles() == ["new one"]


async def test_errors_back_off_and_notify_once(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    watch = await add(store, site, clock, path="/gone.rss")
    scheduler = make_scheduler(store, fetcher, notifier, clock)
    gaps = []
    for _ in range(NOTIFY_AFTER_ERRORS + 1):
        await scheduler.run_once()
        stored = await store.get_watch(watch.id)
        assert stored is not None
        assert stored.next_due_at is not None
        gaps.append((stored.next_due_at - clock.now).total_seconds() / 60)
        clock.now = stored.next_due_at
    assert gaps == [5, 10, 20, 40]
    assert len(notifier.failing) == 1
    assert "HTTP 404" in notifier.failing[0][1]

    site.set("/gone.rss", rss(("1", "back", "")), "application/rss+xml")
    await scheduler.run_once()
    stored = await store.get_watch(watch.id)
    assert stored is not None
    assert (stored.error_count, stored.last_error) == (0, "")


async def test_ai_filter_drops_irrelevant_and_fails_open(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    site.set("/jobs.rss", rss(("1", "seed", "")), "application/rss+xml")
    await add(store, site, clock, criteria="remote roles only")
    relevance = FakeRelevance()
    scheduler = make_scheduler(store, fetcher, notifier, clock, relevance)
    await scheduler.run_once()
    assert relevance.calls == []  # baseline items are never sent to the model

    site.set(
        "/jobs.rss",
        rss(
            ("2", "Bot dev", "Fully remote"),
            ("3", "Office job", "On-site in Berlin"),
            ("4", "Mystery", "???"),
            ("1", "seed", ""),
        ),
        "application/rss+xml",
    )
    clock.advance(6)
    await scheduler.run_once()
    sent = {alert.item.title: (alert.ai_status, alert.ai_reason) for _, alert in notifier.alerts}
    assert sent == {
        "Bot dev": (AiStatus.PASSED, "remote role"),
        "Mystery": (AiStatus.UNCHECKED, "AI check unavailable"),
    }

    clock.advance(6)
    await scheduler.run_once()
    assert sorted(relevance.calls) == ["Bot dev", "Mystery", "Office job"]  # rejected stays seen


async def test_html_watch_end_to_end(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    page = fixture_bytes("listings.html").decode()
    site.set("/s", page.replace('<li class="card">', '<li class="old">', 1))
    spec = ParseSpec(items="li.card", fields={"title": ".card__title", "price": ".card__price"})
    await add(
        store,
        site,
        clock,
        path="/s",
        kind=WatchKind.HTML,
        spec=spec,
        keywords=parse_keywords("bicycle, bike*, -kids"),
    )
    scheduler = make_scheduler(store, fetcher, notifier, clock)
    await scheduler.run_once()
    site.set("/s", page)  # the first card "appears"
    clock.advance(6)
    await scheduler.run_once()
    assert [(a.item.title, a.item.fields.get("price")) for _, a in notifier.alerts] == [
        ('Used bicycle, 26"', "120 USD"),
    ]


async def test_preview_shows_current_matches_without_side_effects(
    site: Site, store: Store, fetcher: Fetcher, clock: Clock
) -> None:
    site.set("/jobs.rss", fixture_bytes("feed.rss"), "application/rss+xml")
    watch = await add(store, site, clock, keywords=parse_keywords("python, react"))
    engine = WatchEngine(store, fetcher, FakeRelevance())
    result = await engine.preview(watch, limit=5)
    assert result.items_found == 3
    assert [a.item.title for a in result.alerts] == [
        "Python developer for a Telegram bot",
        "JavaScript widget fix",
    ]
    assert await store.unseen_keys(watch.id, ["anything"]) == {"anything"}
    assert (await store.stats()).seen_items == 0


async def test_empty_selector_result_warns(
    site: Site, store: Store, fetcher: Fetcher, clock: Clock
) -> None:
    site.set("/s", "<html><body><p>redesigned</p></body></html>")
    watch = await add(
        store, site, clock, path="/s", kind=WatchKind.HTML, spec=ParseSpec(items="li.card")
    )
    result = await WatchEngine(store, fetcher).preview(watch)
    assert "matched nothing" in result.warning


async def test_watch_deleted_mid_check_does_not_break_the_tick(
    site: Site, store: Store, fetcher: Fetcher, notifier: FakeNotifier, clock: Clock
) -> None:
    site.set("/jobs.rss", rss(("1", "a", "")), "application/rss+xml")
    doomed = await add(store, site, clock)
    healthy = await add(store, site, clock)

    engine = WatchEngine(store, fetcher)
    original = engine.check

    async def check_then_delete(watch: Watch):  # type: ignore[no-untyped-def]
        result = await original(watch)
        if watch.id == doomed.id:
            await store.delete_watch(watch.id, chat_id=watch.chat_id)
        return result

    engine.check = check_then_delete  # type: ignore[method-assign]
    scheduler = Scheduler(store, engine, notifier, clock=clock)
    assert await scheduler.run_once() == 2
    stored = await store.get_watch(healthy.id)
    assert stored is not None
    assert stored.baseline_done
