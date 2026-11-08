"""The bot end to end: real aiogram polling against a local fake Bot API, real engine and store.

No BotFather token is involved; the fake server in ``fake_telegram.py`` plays Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any

import pytest
from conftest import RecordingSleep, Site
from fake_telegram import USER, Call, FakeTelegram, serve_fake_telegram

from tgwatch.ai_filter import RelevanceFilter, Verdict
from tgwatch.bot.app import App, open_app
from tgwatch.bot.notifier import TelegramNotifier
from tgwatch.config import Settings
from tgwatch.models import Item
from tgwatch.scheduler import Scheduler

ADMIN_ID = 777
CHAT = USER["id"]

ITEM = """<item><title>{title}</title><link>https://jobs.example.com/{uid}</link>
<guid>{uid}</guid><description>{desc}</description></item>"""


def rss(*items: tuple[str, str, str]) -> str:
    body = "".join(ITEM.format(uid=u, title=escape(t), desc=escape(d)) for u, t, d in items)
    channel = f"<channel><title>Sample jobs</title>{body}</channel>"
    return f'<?xml version="1.0"?><rss version="2.0">{channel}</rss>'


LISTINGS = """<ul>{}</ul>"""
CARD = '<li class="card"><a href="/ad/{uid}"><h3>{title}</h3></a><b class="price">{price}</b></li>'


def listings(*cards: tuple[str, str, str]) -> str:
    return LISTINGS.format("".join(CARD.format(uid=u, title=t, price=p) for u, t, p in cards))


class Clock:
    """Real time plus however far the test has jumped ahead."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, minutes: float) -> None:
        self.offset += timedelta(minutes=minutes)


@dataclass
class Harness:
    fake: FakeTelegram
    app: App
    site: Site
    clock: Clock
    scheduler: Scheduler
    sleeps: RecordingSleep

    async def say(self, text: str, *, expect: int = 1, chat_id: int = CHAT) -> list[Call]:
        before = len(self.fake.visible())
        self.fake.push_text(text, chat_id=chat_id)
        return await self._wait(before, expect)

    async def press(self, call: Call, label: str, *, expect: int = 1) -> list[Call]:
        data = next((d for text, d in call.buttons if label in text), None)
        assert data is not None, f"no button {label!r} in {call.buttons}"
        before = len(self.fake.visible())
        self.fake.push_callback(call.message_id, data)
        return await self._wait(before, expect)

    async def poll_sources(self, *, after_minutes: float = 0) -> list[Call]:
        """Jump ahead and run one scheduler tick; return what it sent. Asserts it checked."""
        self.clock.advance(after_minutes)
        before = len(self.fake.visible())
        checked = await self.scheduler.run_once()
        assert checked > 0, "no watch was due"
        return self.fake.visible()[before:]

    async def _wait(self, before: int, expect: int) -> list[Call]:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                while len(self.fake.visible()) < before + expect:
                    self.fake.activity.clear()
                    await self.fake.activity.wait()
        await asyncio.sleep(0.15)  # anything extra would show up now
        new = self.fake.visible()[before:]
        assert len(new) == expect, "\n---\n".join(f"{c.method}: {c.text}" for c in new)
        return new


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def make_harness(site: Site, tmp_path: Path) -> AsyncIterator[HarnessFactory]:
    started: list[tuple[App, asyncio.Task[None], Any]] = []

    async def factory(relevance: RelevanceFilter | None = None, **overrides: Any) -> Harness:
        fake_cm = serve_fake_telegram()
        fake = await fake_cm.__aenter__()
        values: dict[str, Any] = {
            "telegram_bot_token": "123456:TEST-not-a-real-token",
            "telegram_api_base": fake.base_url,
            "anthropic_api_key": None,
            "admin_chat_ids": [ADMIN_ID],
            "database_path": tmp_path / "bot.sqlite3",
            "allow_private_hosts": True,
            "host_min_interval_seconds": 0,
            "preview_cooldown_seconds": 0,
        }
        settings = Settings(_env_file=None, **(values | overrides))  # type: ignore[call-arg]
        app_cm = open_app(settings, relevance=relevance)
        app = await app_cm.__aenter__()
        task = asyncio.create_task(
            app.dispatcher.start_polling(
                app.bot, handle_signals=False, close_bot_session=False, polling_timeout=1
            )
        )
        started.append((app, task, (app_cm, fake_cm)))
        for _ in range(250):
            if any(c.method == "getMe" for c in fake.calls):
                break
            await asyncio.sleep(0.02)
        clock = Clock()
        sleeps = RecordingSleep()
        notifier = TelegramNotifier(app.bot, app.store, per_chat_interval=0, sleep=sleeps)
        scheduler = Scheduler(app.store, app.engine, notifier, clock=clock)
        return Harness(fake, app, site, clock, scheduler, sleeps)

    yield factory

    for app, task, (app_cm, fake_cm) in started:
        await app.dispatcher.stop_polling()
        await task
        await app_cm.__aexit__(None, None, None)
        await fake_cm.__aexit__(None, None, None)


@pytest.fixture
async def bot(make_harness: HarnessFactory) -> Harness:
    return await make_harness()


async def test_start_explains_commands(bot: Harness) -> None:
    (reply,) = await bot.say("/start")
    assert "/watch" in reply.text
    assert "/list" in reply.text
    assert reply.params["parse_mode"] == "HTML"


async def test_one_line_watch_alerts_only_on_new_matches(bot: Harness) -> None:
    feed = "/jobs.rss"
    bot.site.set(
        feed,
        rss(("1", "Python developer, remote", "async work"), ("2", "Senior Java lead", "onsite")),
        "application/rss+xml",
    )
    checking, saved = await bot.say(f"/watch {bot.site.url(feed)} python, -senior", expect=2)
    assert "Checking" in checking.text
    assert "Watch #1 saved" in saved.text
    assert "2 items found, 1 match" in saved.text
    assert "Python developer, remote" in saved.text
    assert "Senior Java lead" not in saved.text
    assert [label for label, _ in saved.buttons][:3] == ["⏸ Pause", "🧪 Test", "🗑 Delete"]

    assert await bot.poll_sources() == []  # first check: baseline, nothing sent

    bot.site.set(
        feed,
        rss(
            ("3", "Python & <Django> contract", "remote, $40/h"),
            ("4", "Ruby on Rails developer", "remote"),
            ("1", "Python developer, remote", "async work"),
            ("2", "Senior Java lead", "onsite"),
        ),
        "application/rss+xml",
    )
    (alert,) = await bot.poll_sources(after_minutes=20)
    assert alert.chat_id == CHAT
    assert alert.text.startswith("🔔")
    assert '<a href="https://jobs.example.com/3">Python &amp; &lt;Django&gt; contract</a>' in (
        alert.text
    )
    assert alert.params["disable_web_page_preview"] == "true"
    assert await bot.poll_sources(after_minutes=20) == []  # already seen

    (edited,) = await bot.press(alert, "Pause this watch")
    assert edited.method == "editMessageReplyMarkup"
    assert "paused" in bot.fake.answers()[-1].params["text"]
    watch = await bot.app.store.get_watch(1)
    assert watch is not None
    assert watch.paused

    (listing,) = await bot.say("/list")
    assert "⏸ #1" in listing.text

    _dump_transcript(bot.fake)


async def test_card_buttons_pause_test_and_delete(bot: Harness) -> None:
    bot.site.set("/f.rss", rss(("1", "Release 1.0", "notes")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url('/f.rss')}", expect=2)

    (listing,) = await bot.say("/list")
    (card,) = await bot.press(listing, "#1")
    assert card.method == "editMessageText"
    assert "keywords: any new item" in card.text

    (card,) = await bot.press(card, "Pause")
    assert "<b>paused</b>" in card.text
    (card,) = await bot.press(card, "Resume")
    assert "paused" not in card.text

    (dry_run,) = await bot.press(card, "Test")
    assert dry_run.method == "sendMessage"
    assert "Dry run #1" in dry_run.text
    assert "Release 1.0" in dry_run.text

    (confirm,) = await bot.press(card, "Delete")
    assert "Delete watch #1" in confirm.text
    (after,) = await bot.press(confirm, "Yes, delete")
    assert "no watches yet" in after.text
    assert await bot.app.store.get_watch(1) is None


async def test_guided_setup_for_a_web_page(bot: Harness) -> None:
    bot.site.set("/ads", listings(("1", "Road bike", "900 USD"), ("2", "Kids bike", "60 USD")))

    (kinds,) = await bot.say("/watch")
    (ask_url,) = await bot.press(kinds, "Web page")
    assert "link to the page" in ask_url.text
    (ask_items,) = await bot.say(bot.site.url("/ads"))
    assert "CSS selector" in ask_items.text
    (ask_fields,) = await bot.say("li.card")
    assert "name=selector" in ask_fields.text
    (ask_keywords,) = await bot.say("title=h3\nprice=.price")
    assert "keywords" in ask_keywords.text
    (ask_interval,) = await bot.say("bike, -kids")
    (_edited, saved) = await bot.press(ask_interval, "1 h", expect=2)

    assert "2 items found, 1 match" in saved.text
    assert "Road bike" in saved.text
    assert "price: <b>900 USD</b>" in saved.text
    watch = await bot.app.store.get_watch(1)
    assert watch is not None
    assert watch.interval_seconds == 3600
    assert watch.spec.items == "li.card"
    assert watch.spec.fields == {"title": "h3", "price": ".price"}


async def test_guided_setup_for_a_json_api(bot: Harness) -> None:
    body = json.dumps(
        {"data": {"items": [{"id": 1, "name": "Desk lamp", "url": "/p/1", "price": {"usd": 19}}]}}
    )
    bot.site.set("/api", body, "application/json")
    (kinds,) = await bot.say("/watch")
    await bot.press(kinds, "JSON")
    (ask_items,) = await bot.say(bot.site.url("/api"))
    assert "path to the list" in ask_items.text
    (ask_fields,) = await bot.say("data.items")
    assert "name=path" in ask_fields.text
    (ask_keywords,) = await bot.say("price=price.usd")
    (_, ask_interval) = await bot.press(ask_keywords, "Every new item", expect=2)
    (edited, saved) = await bot.press(ask_interval, "6 h", expect=2)
    assert edited.text == "Every 6 h. Checking the source…"
    assert f'<a href="{bot.site.url("/p/1")}">Desk lamp</a>' in saved.text
    assert "price: <b>19</b>" in saved.text


async def test_guided_setup_skip_buttons(bot: Harness) -> None:
    bot.site.set("/f.rss", rss(("1", "Anything", "")), "application/rss+xml")
    (kinds,) = await bot.say("/watch")
    await bot.press(kinds, "RSS")
    (ask_keywords,) = await bot.say(bot.site.url("/f.rss"))
    (removed_buttons, ask_interval) = await bot.press(ask_keywords, "Every new item", expect=2)
    assert removed_buttons.method == "editMessageReplyMarkup"
    (_edited, saved) = await bot.press(ask_interval, "15 min", expect=2)
    assert "1 items found, 1 match" in saved.text


class BudgetJudge:
    """Stands in for Claude: relevant when the summary mentions a budget."""

    async def judge(self, item: Item, criteria: str) -> Verdict | None:
        if "budget" in item.summary:
            return Verdict(relevant=True, reason="Mentions a budget of $500.")
        return Verdict(relevant=False, reason="No budget given.")


async def test_ai_criteria_step_and_verdicts(make_harness: HarnessFactory) -> None:
    bot = await make_harness(relevance=BudgetJudge())
    feed = "/f.rss"
    bot.site.set(feed, rss(("1", "Landing page", "budget $500"), ("2", "Logo", "tbd")), "text/xml")
    (kinds,) = await bot.say("/watch")
    await bot.press(kinds, "RSS")
    await bot.say(bot.site.url(feed))
    (ask_criteria,) = await bot.say("page, logo")
    assert "plain words" in ask_criteria.text
    (ask_interval,) = await bot.say("has a budget")
    (_, saved) = await bot.press(ask_interval, "15 min", expect=2)
    assert "<i>AI: Mentions a budget of $500.</i>" in saved.text
    assert "AI skipped it: No budget given." in saved.text

    await bot.poll_sources()
    bot.site.set(
        feed,
        rss(("3", "Logo refresh", "budget $500"), ("4", "Logo", "free"), ("1", "Landing page", "")),
        "text/xml",
    )
    (alert,) = await bot.poll_sources(after_minutes=20)
    assert "Logo refresh" in alert.text
    assert "AI: Mentions a budget" in alert.text


async def test_guided_setup_rejects_bad_input_and_can_cancel(bot: Harness) -> None:
    (kinds,) = await bot.say("/watch")
    await bot.press(kinds, "JSON")
    (reply,) = await bot.say("ftp://example.com/data")
    assert "Only http:// and https://" in reply.text
    (reply,) = await bot.say("/cancel")
    assert "Nothing was saved" in reply.text
    (reply,) = await bot.say("hello")
    assert "/help" in reply.text


async def test_selector_that_matches_nothing_is_not_saved(bot: Harness) -> None:
    bot.site.set("/ads", listings(("1", "Road bike", "900 USD")))
    (kinds,) = await bot.say("/watch")
    await bot.press(kinds, "Web page")
    await bot.say(bot.site.url("/ads"))
    await bot.say("div.nothing-here")
    (ask_keywords,) = await bot.say("title=h3")
    (_, ask_interval) = await bot.press(ask_keywords, "Every new item", expect=2)
    (_, error) = await bot.press(ask_interval, "15 min", expect=2)
    assert "matched nothing" in error.text
    assert await bot.app.store.count_watches(CHAT) == 0


async def test_unreachable_source_is_explained(bot: Harness) -> None:
    _, error = await bot.say(f"/watch {bot.site.url('/missing.rss')}", expect=2)
    assert "Could not read the source" in error.text
    assert "404" in error.text
    assert await bot.app.store.count_watches(CHAT) == 0


async def test_web_page_given_as_feed_is_not_saved(bot: Harness) -> None:
    bot.site.set("/page", "<html><body><p>Just a page</p></body></html>")
    _, error = await bot.say(f"/watch {bot.site.url('/page')}", expect=2)
    assert "not a valid RSS/Atom feed" in error.text
    assert "pick Web page" in error.text
    assert await bot.app.store.count_watches(CHAT) == 0


async def test_watch_limit_per_chat(make_harness: HarnessFactory) -> None:
    bot = await make_harness(max_watches_per_chat=1)
    bot.site.set("/f.rss", rss(("1", "One", "")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url('/f.rss')}", expect=2)
    (reply,) = await bot.say(f"/watch {bot.site.url('/f.rss')}")
    assert "already have 1 watches" in reply.text
    (reply,) = await bot.say("/watch")
    assert "already have 1 watches" in reply.text


async def test_test_cooldown(make_harness: HarnessFactory) -> None:
    bot = await make_harness(preview_cooldown_seconds=60)
    bot.site.set("/f.rss", rss(("1", "One", "")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url('/f.rss')}", expect=2)
    (reply,) = await bot.say("/test 1")
    assert "Try again in" in reply.text


async def test_commands_only_touch_your_own_watches(bot: Harness) -> None:
    bot.site.set("/f.rss", rss(("1", "One", "")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url('/f.rss')}", expect=2)
    stranger = 5555
    for command in ("/test 1", "/pause 1", "/unwatch 1"):
        (reply,) = await bot.say(command, chat_id=stranger)
        assert "There is no watch #1" in reply.text
    (reply,) = await bot.say("/pause", chat_id=stranger)
    assert "Add the watch number" in reply.text
    watch = await bot.app.store.get_watch(1)
    assert watch is not None
    assert not watch.paused


async def test_pause_and_resume_commands(bot: Harness) -> None:
    bot.site.set("/f.rss", rss(("1", "One", "")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url('/f.rss')}", expect=2)
    (card,) = await bot.say("/pause 1")
    assert "<b>paused</b>" in card.text
    (card,) = await bot.say("/resume #1")
    assert "paused" not in card.text


async def test_stats_is_admin_only(bot: Harness) -> None:
    (reply,) = await bot.say("/stats")
    assert "for the bot admin" in reply.text
    (reply,) = await bot.say("/stats", chat_id=ADMIN_ID)
    assert "Bot stats" in reply.text
    assert "watches: 0" in reply.text


async def test_blocked_user_gets_watches_paused(bot: Harness) -> None:
    feed = "/f.rss"
    bot.site.set(feed, rss(("1", "One", "")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url(feed)}", expect=2)
    await bot.poll_sources()
    bot.site.set(feed, rss(("2", "Two", ""), ("1", "One", "")), "application/rss+xml")
    bot.fake.blocked_chats.add(CHAT)

    assert await bot.poll_sources(after_minutes=20) == []
    watch = await bot.app.store.get_watch(1)
    assert watch is not None
    assert watch.paused
    # Not marked seen: it is delivered if the user unblocks the bot and resumes.
    assert len(await bot.app.store.unseen_keys(1, [_key("2")])) == 1


async def test_flood_limit_waits_and_retries(bot: Harness) -> None:
    feed = "/f.rss"
    bot.site.set(feed, rss(("1", "One", "")), "application/rss+xml")
    await bot.say(f"/watch {bot.site.url(feed)}", expect=2)
    await bot.poll_sources()
    bot.site.set(feed, rss(("2", "Two", ""), ("1", "One", "")), "application/rss+xml")
    bot.fake.flood_next = 3

    (alert,) = await bot.poll_sources(after_minutes=20)
    assert "Two" in alert.text
    assert bot.sleeps.calls == [3.0]


def _key(uid: str) -> str:
    return Item(uid=uid, title="").key


def _dump_transcript(fake: FakeTelegram) -> None:
    """Set TGWATCH_TRANSCRIPT=path to save the chat for the README screenshot."""
    target = os.environ.get("TGWATCH_TRANSCRIPT")
    if not target:
        return
    Path(target).write_text(json.dumps(fake.transcript, indent=2, ensure_ascii=False))
