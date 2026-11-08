"""Inline keyboards and their callback data."""

from __future__ import annotations

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tgwatch.bot.texts import clip, interval_label
from tgwatch.models import Watch, WatchKind

INTERVAL_CHOICES = (15, 60, 360, 1440)
"""Minutes offered in the step-by-step setup."""


class Action(StrEnum):
    OPEN = "open"
    LIST = "list"
    PAUSE = "pause"
    RESUME = "resume"
    TEST = "test"
    DELETE = "del"
    DELETE_CONFIRM = "delok"
    ALERT_PAUSE = "apause"


class WatchCb(CallbackData, prefix="w"):
    action: Action
    watch_id: int = 0


class KindCb(CallbackData, prefix="k"):
    kind: WatchKind


class IntervalCb(CallbackData, prefix="i"):
    minutes: int


class SkipCb(CallbackData, prefix="skip"):
    pass


def watch_list(watches: list[Watch]) -> InlineKeyboardMarkup | None:
    if not watches:
        return None
    builder = InlineKeyboardBuilder()
    for watch in watches:
        mark = "⏸ " if watch.paused else ""
        builder.button(
            text=f"{mark}#{watch.id} {clip(watch.name, 40)}",
            callback_data=WatchCb(action=Action.OPEN, watch_id=watch.id),
        )
    builder.adjust(1)
    return builder.as_markup()


def watch_card(watch: Watch) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    toggle = Action.RESUME if watch.paused else Action.PAUSE
    builder.button(
        text="▶️ Resume" if watch.paused else "⏸ Pause",
        callback_data=WatchCb(action=toggle, watch_id=watch.id),
    )
    builder.button(text="🧪 Test", callback_data=WatchCb(action=Action.TEST, watch_id=watch.id))
    builder.button(text="🗑 Delete", callback_data=WatchCb(action=Action.DELETE, watch_id=watch.id))
    builder.button(text="« All watches", callback_data=WatchCb(action=Action.LIST))
    builder.adjust(3, 1)
    return builder.as_markup()


def confirm_delete(watch: Watch) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Yes, delete",
        callback_data=WatchCb(action=Action.DELETE_CONFIRM, watch_id=watch.id),
    )
    builder.button(text="Keep it", callback_data=WatchCb(action=Action.OPEN, watch_id=watch.id))
    return builder.as_markup()


def alert(watch: Watch) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⏸ Pause this watch",
        callback_data=WatchCb(action=Action.ALERT_PAUSE, watch_id=watch.id),
    )
    return builder.as_markup()


def kinds() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="RSS / Atom feed", callback_data=KindCb(kind=WatchKind.RSS))
    builder.button(text="Web page", callback_data=KindCb(kind=WatchKind.HTML))
    builder.button(text="JSON API", callback_data=KindCb(kind=WatchKind.JSON))
    builder.adjust(1)
    return builder.as_markup()


def skip(label: str = "Skip") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=SkipCb())
    return builder.as_markup()


def intervals(min_minutes: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for minutes in INTERVAL_CHOICES:
        if minutes < min_minutes:
            continue
        builder.button(text=interval_label(minutes * 60), callback_data=IntervalCb(minutes=minutes))
    builder.adjust(4)
    return builder.as_markup()
