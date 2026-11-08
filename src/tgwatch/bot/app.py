"""Wires the bot, the watch engine and the scheduler into one asyncio process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anthropic
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from tgwatch.ai_filter import ClaudeRelevanceFilter, RelevanceFilter
from tgwatch.bot.handlers import build_router
from tgwatch.bot.notifier import TelegramNotifier
from tgwatch.bot.service import PreviewLimiter, WatchService
from tgwatch.config import Settings
from tgwatch.engine import WatchEngine
from tgwatch.fetch import DEFAULT_USER_AGENT, Fetcher
from tgwatch.scheduler import Scheduler
from tgwatch.store import Store

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="watch", description="Add a watch"),
    BotCommand(command="list", description="Your watches"),
    BotCommand(command="test", description="Dry run a watch"),
    BotCommand(command="pause", description="Pause a watch"),
    BotCommand(command="resume", description="Resume a watch"),
    BotCommand(command="unwatch", description="Delete a watch"),
    BotCommand(command="help", description="How it works"),
]


def build_bot(settings: Settings) -> Bot:
    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (get one from @BotFather)")
    session = (
        AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base))
        if settings.telegram_api_base
        else AiohttpSession()
    )
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def build_dispatcher(service: WatchService, settings: Settings) -> Dispatcher:
    dispatcher = Dispatcher(service=service, admin_ids=frozenset(settings.admin_chat_ids))
    dispatcher.include_router(build_router())
    return dispatcher


def build_relevance(settings: Settings) -> RelevanceFilter | None:
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        return None
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    return ClaudeRelevanceFilter(client=client, model=settings.anthropic_model)


@dataclass(slots=True)
class App:
    bot: Bot
    dispatcher: Dispatcher
    scheduler: Scheduler
    store: Store
    engine: WatchEngine


@contextlib.asynccontextmanager
async def open_app(
    settings: Settings, *, relevance: RelevanceFilter | None = None
) -> AsyncIterator[App]:
    """Everything the bot needs, opened in order and closed in reverse.

    ``relevance`` replaces the Claude filter built from the settings (used by tests).
    """
    bot = build_bot(settings)
    async with (
        Store(settings.database_path) as store,
        Fetcher(
            user_agent=settings.user_agent or DEFAULT_USER_AGENT,
            timeout=settings.http_timeout_seconds,
            host_min_interval=settings.host_min_interval_seconds,
            respect_robots=settings.respect_robots_txt,
            allow_private_hosts=settings.allow_private_hosts,
        ) as fetcher,
    ):
        engine = WatchEngine(store, fetcher, relevance or build_relevance(settings))
        service = WatchService(
            store,
            engine,
            max_watches_per_chat=settings.max_watches_per_chat,
            default_interval_minutes=settings.default_interval_minutes,
            limiter=PreviewLimiter(settings.preview_cooldown_seconds),
        )
        scheduler = Scheduler(
            store,
            engine,
            TelegramNotifier(bot, store),
            tick_seconds=settings.scheduler_tick_seconds,
        )
        try:
            yield App(bot, build_dispatcher(service, settings), scheduler, store, engine)
        finally:
            await bot.session.close()


async def run(settings: Settings) -> None:
    """Long-poll Telegram and run the scheduler until SIGINT/SIGTERM."""
    async with open_app(settings) as app:
        me = await app.bot.get_me()
        log.info(
            "running as @%s, AI filter %s",
            me.username,
            "on" if settings.anthropic_api_key else "off",
        )
        await app.bot.set_my_commands(COMMANDS)
        stop = asyncio.Event()
        scheduler_task = asyncio.create_task(app.scheduler.run_forever(stop))
        try:
            # aiogram installs SIGINT/SIGTERM handlers and returns once polling has stopped.
            await app.dispatcher.start_polling(
                app.bot, handle_signals=True, close_bot_session=False
            )
        finally:
            stop.set()
            await scheduler_task
