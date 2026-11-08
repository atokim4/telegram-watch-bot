"""Sends alerts through the Bot API, within Telegram's flood limits."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from tgwatch.bot import keyboards, texts
from tgwatch.engine import Alert
from tgwatch.models import Watch
from tgwatch.store import Store

log = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 60.0
PER_CHAT_INTERVAL_SECONDS = 1.0
"""Telegram allows about one message per second to the same private chat."""


class ChatUnreachableError(Exception):
    """The user blocked the bot or deleted the chat; their watches were paused."""


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        store: Store,
        *,
        per_chat_interval: float = PER_CHAT_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._store = store
        self._interval = per_chat_interval
        self._sleep = sleep
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_sent: dict[int, float] = {}

    async def send_alert(self, watch: Watch, alert: Alert) -> None:
        await self._send(watch, texts.alert_text(watch, alert), keyboards.alert(watch))

    async def send_watch_failing(self, watch: Watch, error: str) -> None:
        await self._send(watch, texts.watch_failing_text(watch, error), None)

    async def _send(self, watch: Watch, text: str, markup: InlineKeyboardMarkup | None) -> None:
        chat_id = watch.chat_id
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            wait = self._last_sent.get(chat_id, float("-inf")) + self._interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            try:
                await self._send_with_retry(chat_id, text, markup)
            except TelegramForbiddenError as exc:
                paused = await self._store.pause_chat(chat_id)
                log.info("chat %s unreachable, paused %d watches", chat_id, paused)
                raise ChatUnreachableError(str(exc)) from exc
            finally:
                self._last_sent[chat_id] = time.monotonic()

    async def _send_with_retry(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> None:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramRetryAfter as exc:
            if exc.retry_after > MAX_RETRY_AFTER_SECONDS:
                raise  # the scheduler keeps the item unseen and tries on the next poll
            log.warning("flood limit, waiting %s s", exc.retry_after)
            await self._sleep(float(exc.retry_after))
            await self._bot.send_message(chat_id, text, reply_markup=markup)
