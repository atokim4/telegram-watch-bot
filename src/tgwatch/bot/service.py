"""What the bot does, without the Telegram parts: validation, limits, create and test watches."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from tgwatch.engine import CheckResult, WatchEngine
from tgwatch.errors import ParseError, WatchError
from tgwatch.filters import parse_keywords
from tgwatch.models import MIN_INTERVAL_SECONDS, ParseSpec, Watch, WatchKind
from tgwatch.store import NewWatch, Store

MAX_URL_LENGTH = 2048
MAX_SELECTOR_LENGTH = 300
MAX_FIELDS = 8
MAX_CRITERIA_LENGTH = 500
PREVIEW_COOLDOWN_SECONDS = 10.0
"""Each /test or new watch fetches the source; one per chat every few seconds is plenty."""


class UserError(Exception):
    """A problem with what the user typed. The message is shown to them as is."""


@dataclass(slots=True)
class Draft:
    """A watch being set up, before it is saved."""

    url: str = ""
    kind: WatchKind = WatchKind.RSS
    items: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    keywords: str = ""
    criteria: str = ""
    interval_minutes: int = 15


def validate_url(text: str) -> str:
    url = text.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(ch.isspace() for ch in url):
        raise UserError("That does not look like a link. Send a full URL starting with https://")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UserError("Only http:// and https:// links can be watched.")
    return url


def validate_selector(text: str, kind: WatchKind) -> str:
    selector = text.strip()
    what = "CSS selector" if kind is WatchKind.HTML else "JSON path"
    if not selector:
        raise UserError(f"Send a {what} for the items.")
    if len(selector) > MAX_SELECTOR_LENGTH:
        raise UserError(f"That {what} is too long.")
    return selector


def parse_field_lines(text: str) -> dict[str, str]:
    """``title=h2 a`` lines (or ``;`` separated) to a dict. Selectors may contain commas."""
    fields: dict[str, str] = {}
    for raw in text.replace(";", "\n").splitlines():
        if not raw.strip():
            continue
        name, sep, selector = raw.partition("=")
        name, selector = name.strip().lower(), selector.strip()
        if not sep or not name or not selector:
            raise UserError(f"Write each field as name=selector, got: {raw.strip()[:60]}")
        if not name.replace("_", "").isalnum() or len(name) > 30:
            raise UserError(f"Field names are short words like title, link or price: {name[:30]}")
        if len(selector) > MAX_SELECTOR_LENGTH:
            raise UserError(f"The selector for {name} is too long.")
        fields[name] = selector
    if len(fields) > MAX_FIELDS:
        raise UserError(f"At most {MAX_FIELDS} fields per watch.")
    return fields


def watch_name(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path.rstrip("/")
    name = host + path
    return name if len(name) <= 60 else name[:59] + "…"


class PreviewLimiter:
    def __init__(
        self,
        cooldown: float = PREVIEW_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._last: dict[int, float] = {}

    def acquire(self, chat_id: int) -> None:
        now = self._clock()
        last = self._last.get(chat_id)
        if last is not None and now - last < self._cooldown:
            wait = math.ceil(self._cooldown - (now - last))
            raise UserError(f"One check at a time, please. Try again in {wait} s.")
        self._last[chat_id] = now


class WatchService:
    def __init__(
        self,
        store: Store,
        engine: WatchEngine,
        *,
        max_watches_per_chat: int,
        default_interval_minutes: int = 15,
        limiter: PreviewLimiter | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.max_watches_per_chat = max_watches_per_chat
        self.default_interval_minutes = default_interval_minutes
        self._limiter = limiter or PreviewLimiter()

    @property
    def ai_enabled(self) -> bool:
        return self.engine.ai_enabled

    async def ensure_room(self, chat_id: int) -> None:
        if await self.store.count_watches(chat_id) >= self.max_watches_per_chat:
            raise UserError(
                f"You already have {self.max_watches_per_chat} watches, the limit for this bot. "
                "Delete one with /unwatch first."
            )

    async def create(self, chat_id: int, draft: Draft) -> tuple[Watch, CheckResult]:
        """Check the source works, then save the watch. Nothing is saved if the check fails."""
        await self.ensure_room(chat_id)
        url = validate_url(draft.url)
        criteria = " ".join(draft.criteria.split())
        if len(criteria) > MAX_CRITERIA_LENGTH:
            raise UserError(f"Keep the AI criteria under {MAX_CRITERIA_LENGTH} characters.")
        interval = max(draft.interval_minutes * 60, MIN_INTERVAL_SECONDS)
        new = NewWatch(
            chat_id=chat_id,
            name=watch_name(url),
            url=url,
            kind=draft.kind,
            spec=ParseSpec(items=draft.items, fields=dict(draft.fields)),
            keywords=parse_keywords(draft.keywords),
            criteria=criteria,
            interval_seconds=interval,
        )
        probe = Watch(
            id=0,
            chat_id=chat_id,
            name=new.name,
            url=new.url,
            kind=new.kind,
            spec=new.spec,
            keywords=new.keywords,
            criteria=new.criteria,
            interval_seconds=interval,
        )
        result = await self.preview(probe)
        if result.items_found == 0:
            raise UserError(
                "The link loaded, but I found no feed entries there. If it is a web page, "
                "send /watch and pick Web page."
                if draft.kind is WatchKind.RSS
                else "The source loaded, but the selector matched nothing. "
                "Check the selector and start again with /watch."
            )
        return await self.store.add_watch(new), result

    async def preview(self, watch: Watch) -> CheckResult:
        self._limiter.acquire(watch.chat_id)
        try:
            return await self.engine.preview(watch)
        except ParseError as exc:
            hint = (
                " If it is a web page, send /watch and pick Web page."
                if watch.kind is WatchKind.RSS
                else ""
            )
            raise UserError(f"Could not read the source: {exc}.{hint}") from exc
        except WatchError as exc:
            raise UserError(f"Could not read the source: {exc}") from exc

    async def owned(self, chat_id: int, watch_id: int) -> Watch:
        watch = await self.store.get_watch(watch_id, chat_id=chat_id)
        if watch is None:
            raise UserError(f"There is no watch #{watch_id} in this chat. See /list.")
        return watch
