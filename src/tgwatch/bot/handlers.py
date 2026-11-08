"""Telegram commands, the step-by-step /watch form and inline-button callbacks."""

from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import (
    Command,
    CommandObject,
    CommandStart,
    ExceptionTypeFilter,
    StateFilter,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardMarkup, Message

from tgwatch.bot import keyboards, texts
from tgwatch.bot.keyboards import Action, IntervalCb, KindCb, SkipCb, WatchCb
from tgwatch.bot.service import (
    Draft,
    UserError,
    WatchService,
    parse_field_lines,
    validate_selector,
    validate_url,
)
from tgwatch.engine import CheckResult
from tgwatch.models import MIN_INTERVAL_SECONDS, Watch, WatchKind

log = logging.getLogger(__name__)


class WatchForm(StatesGroup):
    kind = State()
    url = State()
    items = State()
    fields = State()
    keywords = State()
    criteria = State()
    interval = State()


ITEMS_PROMPT = {
    WatchKind.HTML: (
        "Send the <b>CSS selector</b> that matches one element per item, "
        "e.g. <code>li.listing</code> or <code>article.post</code>."
    ),
    WatchKind.JSON: (
        "Send the <b>path to the list of items</b>, e.g. <code>data.items</code> or "
        "<code>$[*]</code> when the response is the list itself."
    ),
}
FIELDS_PROMPT = {
    WatchKind.HTML: (
        "Optional: fields inside one item, one per line as <code>name=selector</code>:\n"
        "<code>title=h2 a\nlink=h2 a\nprice=.price</code>\n"
        "Title and link are guessed when you skip this."
    ),
    WatchKind.JSON: (
        "Optional: fields inside one item, one per line as <code>name=path</code>:\n"
        "<code>title=name\nlink=html_url\nprice=price.amount</code>\n"
        "Common keys like title, url and id are found when you skip this."
    ),
}
KEYWORDS_PROMPT = (
    "Send <b>keywords</b>, e.g. <code>python, remote, -senior</code>, "
    "or skip to get every new item."
)
CRITERIA_PROMPT = (
    "Optional: describe in plain words what you want, and the AI checks every new item, "
    "e.g. <i>remote job for a Python developer, budget over $500</i>."
)


async def _reply_card(message: Message, watch: Watch) -> None:
    await message.answer(texts.watch_card(watch), reply_markup=keyboards.watch_card(watch))


def _parse_id(command: CommandObject) -> int:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        raise UserError(f"Add the watch number, e.g. /{command.command} 3. See /list.")
    return int(arg)


def _created_text(watch: Watch, result: CheckResult) -> str:
    sample = texts.preview_text(result, title=f"Watch #{watch.id} saved: {watch.name}")
    if not result.alerts:
        return f"{sample}\n\nNothing matches right now. I will message you when a new item does."
    return (
        f"{sample}\n\nThat is what matches right now. From the next check on, I message you "
        "only about <b>new</b> items like these."
    )


# --- commands ---------------------------------------------------------------------------


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.HELP)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("Cancelled. Nothing was saved.")


async def cmd_watch(
    message: Message, command: CommandObject, state: FSMContext, service: WatchService
) -> None:
    await state.clear()
    chat_id = message.chat.id
    await service.ensure_room(chat_id)
    if not command.args:
        await state.set_state(WatchForm.kind)
        await message.answer("What should I watch?", reply_markup=keyboards.kinds())
        return
    url, _, keywords = command.args.strip().partition(" ")
    draft = Draft(
        url=url,
        kind=WatchKind.RSS,
        keywords=keywords,
        interval_minutes=service.default_interval_minutes,
    )
    await message.answer("Checking the feed…")
    watch, result = await service.create(chat_id, draft)
    await message.answer(_created_text(watch, result), reply_markup=keyboards.watch_card(watch))


async def cmd_list(message: Message, service: WatchService) -> None:
    watches = await service.store.list_watches(message.chat.id)
    await message.answer(
        texts.list_text(watches, service.max_watches_per_chat),
        reply_markup=keyboards.watch_list(watches),
    )


async def cmd_pause(message: Message, command: CommandObject, service: WatchService) -> None:
    watch = await service.owned(message.chat.id, _parse_id(command))
    paused = command.command == "pause"
    await service.store.set_paused(watch.id, chat_id=watch.chat_id, paused=paused)
    await _reply_card(message, await service.owned(watch.chat_id, watch.id))


async def cmd_unwatch(message: Message, command: CommandObject, service: WatchService) -> None:
    watch = await service.owned(message.chat.id, _parse_id(command))
    await message.answer(
        f"Delete watch #{watch.id} <b>{escape(watch.name)}</b>? Its history goes too.",
        reply_markup=keyboards.confirm_delete(watch),
    )


async def cmd_test(message: Message, command: CommandObject, service: WatchService) -> None:
    watch = await service.owned(message.chat.id, _parse_id(command))
    result = await service.preview(watch)
    await message.answer(texts.preview_text(result, title=f"Dry run #{watch.id}: {watch.name}"))


async def cmd_stats(message: Message, service: WatchService, admin_ids: frozenset[int]) -> None:
    if message.from_user is None or message.from_user.id not in admin_ids:
        await message.answer("That command is for the bot admin.")
        return
    await message.answer(texts.stats_text(await service.store.stats()))


# --- step-by-step /watch ----------------------------------------------------------------


async def form_kind(callback: CallbackQuery, callback_data: KindCb, state: FSMContext) -> None:
    await state.update_data(kind=callback_data.kind.value)
    await state.set_state(WatchForm.url)
    await callback.answer()
    if isinstance(callback.message, Message):
        what = {"rss": "feed", "html": "page", "json": "API endpoint"}[callback_data.kind.value]
        await callback.message.edit_text(f"Send the link to the {what}.")


async def form_url(message: Message, state: FSMContext) -> None:
    url = validate_url(message.text or "")
    data = await state.update_data(url=url)
    kind = WatchKind(data["kind"])
    if kind is WatchKind.RSS:
        await state.set_state(WatchForm.keywords)
        await message.answer(KEYWORDS_PROMPT, reply_markup=keyboards.skip("Every new item"))
    else:
        await state.set_state(WatchForm.items)
        await message.answer(ITEMS_PROMPT[kind])


async def form_items(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind = WatchKind(data["kind"])
    items = validate_selector(message.text or "", kind)
    await state.update_data(items=items)
    await state.set_state(WatchForm.fields)
    await message.answer(FIELDS_PROMPT[kind], reply_markup=keyboards.skip())


async def form_fields(message: Message, state: FSMContext) -> None:
    await state.update_data(fields=parse_field_lines(message.text or ""))
    await _ask_keywords(message, state)


async def form_keywords(message: Message, state: FSMContext, service: WatchService) -> None:
    await state.update_data(keywords=message.text or "")
    await _after_keywords(message, state, service)


async def form_criteria(message: Message, state: FSMContext) -> None:
    await state.update_data(criteria=message.text or "")
    await _ask_interval(message, state)


async def form_skip(callback: CallbackQuery, state: FSMContext, service: WatchService) -> None:
    await callback.answer()
    current = await state.get_state()
    message = callback.message
    if not isinstance(message, Message) or current is None:
        return
    await message.edit_reply_markup(reply_markup=None)
    if current == WatchForm.fields.state:
        await _ask_keywords(message, state)
    elif current == WatchForm.keywords.state:
        await _after_keywords(message, state, service)
    elif current == WatchForm.criteria.state:
        await _ask_interval(message, state)


async def form_interval(
    callback: CallbackQuery, callback_data: IntervalCb, state: FSMContext, service: WatchService
) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    data = await state.get_data()
    await state.clear()
    draft = Draft(
        url=data["url"],
        kind=WatchKind(data["kind"]),
        items=data.get("items", ""),
        fields=data.get("fields", {}),
        keywords=data.get("keywords", ""),
        criteria=data.get("criteria", ""),
        interval_minutes=callback_data.minutes,
    )
    every = texts.interval_label(callback_data.minutes * 60)
    await message.edit_text(f"Every {every}. Checking the source…")
    watch, result = await service.create(message.chat.id, draft)
    await message.answer(_created_text(watch, result), reply_markup=keyboards.watch_card(watch))


async def form_not_text(message: Message) -> None:
    await message.answer("Please send that as text, or /cancel.")


async def _ask_keywords(message: Message, state: FSMContext) -> None:
    await state.set_state(WatchForm.keywords)
    await message.answer(KEYWORDS_PROMPT, reply_markup=keyboards.skip("Every new item"))


async def _after_keywords(message: Message, state: FSMContext, service: WatchService) -> None:
    if service.ai_enabled:
        await state.set_state(WatchForm.criteria)
        await message.answer(CRITERIA_PROMPT, reply_markup=keyboards.skip("No AI check"))
    else:
        await _ask_interval(message, state)


async def _ask_interval(message: Message, state: FSMContext) -> None:
    await state.set_state(WatchForm.interval)
    await message.answer(
        "How often should I check?",
        reply_markup=keyboards.intervals(MIN_INTERVAL_SECONDS // 60),
    )


# --- buttons on watch cards and alerts --------------------------------------------------


def _callback_target(callback: CallbackQuery) -> tuple[Message, int] | None:
    message = callback.message
    if not isinstance(message, Message):
        return None  # too old for Telegram to let us edit it
    return message, message.chat.id


async def _edit(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        # A double tap asks for the same text again; Telegram refuses that edit.
        if "message is not modified" not in exc.message:
            raise


async def on_watch_button(
    callback: CallbackQuery, callback_data: WatchCb, service: WatchService
) -> None:
    target = _callback_target(callback)
    if target is None:
        await callback.answer("This message is too old. Use /list.", show_alert=True)
        return
    message, chat_id = target
    action = callback_data.action

    if action is Action.LIST:
        await callback.answer()
        watches = await service.store.list_watches(chat_id)
        await _edit(
            message,
            texts.list_text(watches, service.max_watches_per_chat),
            keyboards.watch_list(watches),
        )
        return

    try:
        watch = await service.owned(chat_id, callback_data.watch_id)
    except UserError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    if action is Action.OPEN:
        await callback.answer()
        await _edit(message, texts.watch_card(watch), keyboards.watch_card(watch))
    elif action in {Action.PAUSE, Action.RESUME}:
        paused = action is Action.PAUSE
        await service.store.set_paused(watch.id, chat_id=chat_id, paused=paused)
        await callback.answer(f"#{watch.id} {'paused' if paused else 'resumed'}")
        watch = await service.owned(chat_id, watch.id)
        await _edit(message, texts.watch_card(watch), keyboards.watch_card(watch))
    elif action is Action.ALERT_PAUSE:
        await service.store.set_paused(watch.id, chat_id=chat_id, paused=True)
        await callback.answer(f"#{watch.id} paused. /resume {watch.id} turns it back on.")
        await message.edit_reply_markup(reply_markup=None)
    elif action is Action.TEST:
        await callback.answer("Checking…")
        try:
            result = await service.preview(watch)
        except UserError as exc:
            await message.answer(escape(str(exc)))
            return
        await message.answer(texts.preview_text(result, title=f"Dry run #{watch.id}: {watch.name}"))
    elif action is Action.DELETE:
        await callback.answer()
        await _edit(
            message,
            f"Delete watch #{watch.id} <b>{escape(watch.name)}</b>? Its history goes too.",
            keyboards.confirm_delete(watch),
        )
    elif action is Action.DELETE_CONFIRM:
        await service.store.delete_watch(watch.id, chat_id=chat_id)
        await callback.answer(f"#{watch.id} deleted")
        watches = await service.store.list_watches(chat_id)
        await _edit(
            message,
            texts.list_text(watches, service.max_watches_per_chat),
            keyboards.watch_list(watches),
        )


async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("This button is no longer active.")


async def fallback(message: Message) -> None:
    await message.answer("I did not get that. /help shows what I can do.")


# --- errors -----------------------------------------------------------------------------


async def on_user_error(event: ErrorEvent) -> None:
    text = escape(str(event.exception))
    update = event.update
    if update.message is not None:
        await update.message.answer(text)
    elif update.callback_query is not None and isinstance(update.callback_query.message, Message):
        await update.callback_query.message.answer(text)


async def on_unexpected_error(event: ErrorEvent) -> None:
    log.error("update %s failed", event.update.update_id, exc_info=event.exception)
    if event.update.message is not None:
        await event.update.message.answer("Something went wrong on my side. Please try again.")


def build_router() -> Router:
    """A fresh router per dispatcher (aiogram attaches a router to one parent only)."""
    router = Router(name="tgwatch")
    # Watches belong to a person; group chats are out of scope for now.
    router.message.filter(F.chat.type == "private")
    # Order matters: commands win over the form steps, catch-alls come last.
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_start, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_watch, Command("watch"))
    router.message.register(cmd_list, Command("list"))
    router.message.register(cmd_pause, Command("pause", "resume"))
    router.message.register(cmd_unwatch, Command("unwatch"))
    router.message.register(cmd_test, Command("test"))
    router.message.register(cmd_stats, Command("stats"))
    router.callback_query.register(form_kind, WatchForm.kind, KindCb.filter())
    router.message.register(form_url, WatchForm.url, F.text)
    router.message.register(form_items, WatchForm.items, F.text)
    router.message.register(form_fields, WatchForm.fields, F.text)
    router.message.register(form_keywords, WatchForm.keywords, F.text)
    router.message.register(form_criteria, WatchForm.criteria, F.text)
    router.callback_query.register(form_skip, SkipCb.filter())
    router.callback_query.register(form_interval, WatchForm.interval, IntervalCb.filter())
    router.message.register(
        form_not_text,
        StateFilter(
            WatchForm.url, WatchForm.items, WatchForm.fields, WatchForm.keywords, WatchForm.criteria
        ),
    )
    router.callback_query.register(on_watch_button, WatchCb.filter())
    router.callback_query.register(stale_button)
    router.message.register(fallback)
    router.errors.register(on_user_error, ExceptionTypeFilter(UserError))
    router.errors.register(on_unexpected_error)
    return router
