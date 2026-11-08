"""SQLite persistence for watches and the hashes of items already seen."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite

from tgwatch.models import MIN_INTERVAL_SECONDS, KeywordRule, ParseSpec, Watch, WatchKind

SCHEMA_VERSION = 1
SEEN_ITEMS_KEPT_PER_WATCH = 5000

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS watches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    url              TEXT    NOT NULL,
    kind             TEXT    NOT NULL CHECK (kind IN ('rss', 'html', 'json')),
    spec_json        TEXT    NOT NULL DEFAULT '{{}}',
    include_json     TEXT    NOT NULL DEFAULT '[]',
    exclude_json     TEXT    NOT NULL DEFAULT '[]',
    criteria         TEXT    NOT NULL DEFAULT '',
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds >= {MIN_INTERVAL_SECONDS}),
    paused           INTEGER NOT NULL DEFAULT 0,
    baseline_done    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    next_due_at      TEXT,
    last_checked_at  TEXT,
    error_count      INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT    NOT NULL DEFAULT '',
    etag             TEXT    NOT NULL DEFAULT '',
    last_modified    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS watches_chat ON watches (chat_id);
CREATE INDEX IF NOT EXISTS watches_due ON watches (paused, next_due_at);

CREATE TABLE IF NOT EXISTS seen_items (
    watch_id      INTEGER NOT NULL REFERENCES watches (id) ON DELETE CASCADE,
    item_key      TEXT    NOT NULL,
    first_seen_at TEXT    NOT NULL,
    PRIMARY KEY (watch_id, item_key)
) WITHOUT ROWID;
"""


@dataclass(frozen=True, slots=True)
class NewWatch:
    chat_id: int
    name: str
    url: str
    kind: WatchKind
    spec: ParseSpec = field(default_factory=ParseSpec)
    keywords: KeywordRule = field(default_factory=KeywordRule)
    criteria: str = ""
    interval_seconds: int = MIN_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class Stats:
    watches: int
    active_watches: int
    chats: int
    seen_items: int
    failing_watches: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._migrate()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not open")
        return self._db

    async def _migrate(self) -> None:
        async with self.db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        version = int(row[0]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{version} is newer than this app (v1)")
        if version < SCHEMA_VERSION:
            await self.db.executescript(_SCHEMA)
            await self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.db.commit()

    # --- watches -------------------------------------------------------------------------

    async def add_watch(self, new: NewWatch, *, now: datetime | None = None) -> Watch:
        if new.interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_SECONDS} seconds")
        created = now or utcnow()
        cursor = await self.db.execute(
            """INSERT INTO watches (chat_id, name, url, kind, spec_json, include_json,
                   exclude_json, criteria, interval_seconds, created_at, next_due_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new.chat_id,
                new.name,
                new.url,
                new.kind.value,
                json.dumps({"items": new.spec.items, "fields": new.spec.fields}),
                json.dumps(list(new.keywords.include)),
                json.dumps(list(new.keywords.exclude)),
                new.criteria,
                new.interval_seconds,
                _iso(created),
                _iso(created),
            ),
        )
        await self.db.commit()
        watch_id = cursor.lastrowid
        assert watch_id is not None
        watch = await self.get_watch(watch_id)
        assert watch is not None
        return watch

    async def get_watch(self, watch_id: int, *, chat_id: int | None = None) -> Watch | None:
        sql = "SELECT * FROM watches WHERE id = ?"
        params: list[Any] = [watch_id]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return _watch_from_row(row) if row else None

    async def list_watches(self, chat_id: int) -> list[Watch]:
        async with self.db.execute(
            "SELECT * FROM watches WHERE chat_id = ? ORDER BY id", (chat_id,)
        ) as cursor:
            return [_watch_from_row(row) for row in await cursor.fetchall()]

    async def count_watches(self, chat_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM watches WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def delete_watch(self, watch_id: int, *, chat_id: int) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM watches WHERE id = ? AND chat_id = ?", (watch_id, chat_id)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def set_paused(
        self, watch_id: int, *, chat_id: int, paused: bool, now: datetime | None = None
    ) -> bool:
        # Resuming makes the watch due right away and clears the error backoff.
        cursor = await self.db.execute(
            """UPDATE watches
                  SET paused = ?,
                      next_due_at = CASE WHEN ? THEN next_due_at ELSE ? END,
                      error_count = CASE WHEN ? THEN error_count ELSE 0 END
                WHERE id = ? AND chat_id = ?""",
            (int(paused), int(paused), _iso(now or utcnow()), int(paused), watch_id, chat_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def pause_chat(self, chat_id: int) -> int:
        """Pause every watch of a chat (e.g. the user blocked the bot). Returns how many."""
        cursor = await self.db.execute(
            "UPDATE watches SET paused = 1 WHERE chat_id = ? AND paused = 0", (chat_id,)
        )
        await self.db.commit()
        return cursor.rowcount

    async def due_watches(self, now: datetime, *, limit: int = 50) -> list[Watch]:
        async with self.db.execute(
            """SELECT * FROM watches
                WHERE paused = 0 AND (next_due_at IS NULL OR next_due_at <= ?)
                ORDER BY next_due_at LIMIT ?""",
            (_iso(now), limit),
        ) as cursor:
            return [_watch_from_row(row) for row in await cursor.fetchall()]

    async def record_success(
        self,
        watch_id: int,
        *,
        checked_at: datetime,
        next_due_at: datetime,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        """Store a good check. ``None`` validators keep the stored ones."""
        await self.db.execute(
            """UPDATE watches
                  SET last_checked_at = ?, next_due_at = ?, error_count = 0, last_error = '',
                      baseline_done = 1,
                      etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified)
                WHERE id = ?""",
            (_iso(checked_at), _iso(next_due_at), etag, last_modified, watch_id),
        )
        await self.db.commit()

    async def record_failure(
        self, watch_id: int, *, checked_at: datetime, next_due_at: datetime, error: str
    ) -> int:
        """Store a failed check and return the new consecutive error count."""
        async with self.db.execute(
            """UPDATE watches
                  SET last_checked_at = ?, next_due_at = ?, error_count = error_count + 1,
                      last_error = ?
                WHERE id = ? RETURNING error_count""",
            (_iso(checked_at), _iso(next_due_at), error[:500], watch_id),
        ) as cursor:
            row = await cursor.fetchone()
        await self.db.commit()
        return int(row[0]) if row else 0

    # --- seen items ----------------------------------------------------------------------

    async def unseen_keys(self, watch_id: int, keys: Sequence[str]) -> set[str]:
        if not keys:
            return set()
        seen: set[str] = set()
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            async with self.db.execute(
                f"SELECT item_key FROM seen_items WHERE watch_id = ? AND item_key IN ({marks})",  # noqa: S608 - only placeholders are interpolated
                (watch_id, *chunk),
            ) as cursor:
                seen.update(row[0] for row in await cursor.fetchall())
        return set(keys) - seen

    async def mark_seen(
        self, watch_id: int, keys: Iterable[str], *, now: datetime | None = None
    ) -> None:
        stamp = _iso(now or utcnow())
        rows = [(watch_id, key, stamp) for key in keys]
        if not rows:
            return
        await self.db.executemany(
            "INSERT OR IGNORE INTO seen_items (watch_id, item_key, first_seen_at) VALUES (?, ?, ?)",
            rows,
        )
        await self.db.execute(
            """DELETE FROM seen_items WHERE watch_id = ? AND item_key IN (
                   SELECT item_key FROM seen_items WHERE watch_id = ?
                    ORDER BY first_seen_at DESC LIMIT -1 OFFSET ?)""",
            (watch_id, watch_id, SEEN_ITEMS_KEPT_PER_WATCH),
        )
        await self.db.commit()

    async def stats(self) -> Stats:
        async with self.db.execute(
            """SELECT COUNT(*), COALESCE(SUM(paused = 0), 0), COUNT(DISTINCT chat_id),
                      COALESCE(SUM(error_count > 0), 0),
                      (SELECT COUNT(*) FROM seen_items)
                 FROM watches"""
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return Stats(
            watches=int(row[0]),
            active_watches=int(row[1]),
            chats=int(row[2]),
            failing_watches=int(row[3]),
            seen_items=int(row[4]),
        )


def _watch_from_row(row: aiosqlite.Row) -> Watch:
    spec = json.loads(row["spec_json"])
    return Watch(
        id=row["id"],
        chat_id=row["chat_id"],
        name=row["name"],
        url=row["url"],
        kind=WatchKind(row["kind"]),
        spec=ParseSpec(items=spec.get("items", ""), fields=dict(spec.get("fields", {}))),
        keywords=KeywordRule(
            include=tuple(json.loads(row["include_json"])),
            exclude=tuple(json.loads(row["exclude_json"])),
        ),
        criteria=row["criteria"],
        interval_seconds=row["interval_seconds"],
        paused=bool(row["paused"]),
        baseline_done=bool(row["baseline_done"]),
        next_due_at=_dt(row["next_due_at"]),
        last_checked_at=_dt(row["last_checked_at"]),
        error_count=row["error_count"],
        last_error=row["last_error"],
        etag=row["etag"],
        last_modified=row["last_modified"],
    )
