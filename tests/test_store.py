from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tgwatch.filters import parse_keywords
from tgwatch.models import ParseSpec, WatchKind
from tgwatch.store import NewWatch, Store

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def new_watch(chat_id: int = 1, **kw: object) -> NewWatch:
    defaults: dict[str, object] = {
        "chat_id": chat_id,
        "name": "Bikes",
        "url": "https://ads.example.com/s?q=bike",
        "kind": WatchKind.HTML,
        "spec": ParseSpec(items="li.card", fields={"price": ".price"}),
        "keywords": parse_keywords("bike, -kids"),
        "criteria": "under 500 USD",
        "interval_seconds": 600,
    }
    defaults.update(kw)
    return NewWatch(**defaults)  # type: ignore[arg-type]


async def test_add_and_read_back_round_trip(store: Store) -> None:
    created = await store.add_watch(new_watch(), now=NOW)
    loaded = await store.get_watch(created.id)
    assert loaded == created
    assert loaded.spec.fields == {"price": ".price"}
    assert loaded.keywords.exclude == ("kids",)
    assert loaded.next_due_at == NOW
    assert not loaded.baseline_done


async def test_interval_floor_is_enforced(store: Store) -> None:
    with pytest.raises(ValueError, match="at least 300"):
        await store.add_watch(new_watch(interval_seconds=60))


async def test_owner_scoping(store: Store) -> None:
    mine = await store.add_watch(new_watch(chat_id=1))
    await store.add_watch(new_watch(chat_id=2))
    assert [w.id for w in await store.list_watches(1)] == [mine.id]
    assert await store.count_watches(2) == 1
    assert await store.get_watch(mine.id, chat_id=2) is None
    assert not await store.delete_watch(mine.id, chat_id=2)
    assert not await store.set_paused(mine.id, chat_id=2, paused=True)
    assert await store.delete_watch(mine.id, chat_id=1)
    assert await store.list_watches(1) == []


async def test_due_pause_resume(store: Store) -> None:
    watch = await store.add_watch(new_watch(), now=NOW)
    assert [w.id for w in await store.due_watches(NOW)] == [watch.id]
    assert await store.due_watches(NOW - timedelta(seconds=1)) == []

    await store.set_paused(watch.id, chat_id=1, paused=True)
    assert await store.due_watches(NOW + timedelta(days=1)) == []

    await store.record_failure(
        watch.id, checked_at=NOW, next_due_at=NOW + timedelta(hours=1), error="boom"
    )
    later = NOW + timedelta(minutes=5)
    await store.set_paused(watch.id, chat_id=1, paused=False, now=later)
    resumed = await store.get_watch(watch.id)
    assert resumed is not None
    assert (resumed.paused, resumed.error_count, resumed.next_due_at) == (False, 0, later)


async def test_success_and_failure_bookkeeping(store: Store) -> None:
    watch = await store.add_watch(new_watch(), now=NOW)
    assert await store.record_failure(watch.id, checked_at=NOW, next_due_at=NOW, error="a") == 1
    assert await store.record_failure(watch.id, checked_at=NOW, next_due_at=NOW, error="b") == 2
    await store.record_success(
        watch.id,
        checked_at=NOW,
        next_due_at=NOW + timedelta(minutes=10),
        etag='"x"',
        last_modified=None,
    )
    loaded = await store.get_watch(watch.id)
    assert loaded is not None
    assert (loaded.error_count, loaded.last_error, loaded.etag, loaded.baseline_done) == (
        0,
        "",
        '"x"',
        True,
    )
    # None keeps the stored validator.
    await store.record_success(
        watch.id, checked_at=NOW, next_due_at=NOW, etag=None, last_modified=None
    )
    assert (await store.get_watch(watch.id)).etag == '"x"'  # type: ignore[union-attr]


async def test_seen_items_dedupe_prune_and_cascade(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tgwatch.store.SEEN_ITEMS_KEPT_PER_WATCH", 3)
    watch = await store.add_watch(new_watch())
    keys = [f"k{i}" for i in range(5)]
    assert await store.unseen_keys(watch.id, keys) == set(keys)
    await store.mark_seen(watch.id, keys[:2], now=NOW)
    await store.mark_seen(watch.id, keys[:2], now=NOW)  # idempotent
    assert await store.unseen_keys(watch.id, keys) == {"k2", "k3", "k4"}

    await store.mark_seen(watch.id, ["k2", "k3"], now=NOW + timedelta(minutes=1))
    # Only the 3 newest are kept: one of the oldest pair was pruned.
    stats = await store.stats()
    assert stats.seen_items == 3
    assert {"k2", "k3"}.isdisjoint(await store.unseen_keys(watch.id, keys))

    await store.delete_watch(watch.id, chat_id=1)
    assert (await store.stats()).seen_items == 0


async def test_unseen_keys_handles_large_batches(store: Store) -> None:
    watch = await store.add_watch(new_watch())
    keys = [f"k{i}" for i in range(1234)]
    await store.mark_seen(watch.id, keys[:1000])
    assert await store.unseen_keys(watch.id, keys) == set(keys[1000:])


async def test_stats(store: Store) -> None:
    a = await store.add_watch(new_watch(chat_id=1))
    await store.add_watch(new_watch(chat_id=1))
    await store.add_watch(new_watch(chat_id=2))
    await store.set_paused(a.id, chat_id=1, paused=True)
    await store.record_failure(a.id, checked_at=NOW, next_due_at=NOW, error="x")
    stats = await store.stats()
    assert (stats.watches, stats.active_watches, stats.chats, stats.failing_watches) == (3, 2, 2, 1)


async def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "db.sqlite3"
    async with Store(path) as first:
        created = await first.add_watch(new_watch())
    async with Store(path) as second:
        assert await second.get_watch(created.id) == created
