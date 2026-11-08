"""Message texts. Everything that comes from users or websites is HTML-escaped here."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from tgwatch.engine import AiStatus, Alert, CheckResult
from tgwatch.filters import format_keywords
from tgwatch.models import Item, Watch, WatchKind
from tgwatch.store import Stats

MESSAGE_LIMIT = 4096
FIELD_LIMIT = 300
TITLE_LIMIT = 300

HELP = """\
<b>I watch feeds and pages and message you when something new matches.</b>

<b>Add a watch</b>
/watch — step by step (RSS feed, web page or JSON API)
/watch <code>&lt;url&gt; [keywords]</code> — RSS/Atom feed in one line, e.g.
<code>/watch https://github.com/aiogram/aiogram/releases.atom fix*, -beta</code>

<b>Manage</b>
/list — your watches, with buttons
/test <code>&lt;id&gt;</code> — dry run: what would alert right now
/pause <code>&lt;id&gt;</code> · /resume <code>&lt;id&gt;</code> · /unwatch <code>&lt;id&gt;</code>
/cancel — stop the current step-by-step setup

<b>Keywords</b>: <code>python, "full stack", -senior, react*</code>
Comma separated. <code>-word</code> excludes, <code>word*</code> matches endings, quotes keep a \
phrase together. Empty means every new item.

The first check only records what is already there, so you get alerts for items that \
appear after that."""


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _item_lines(item: Item) -> list[str]:
    title = escape(clip(item.title or item.link or "(no title)", TITLE_LIMIT))
    lines = [f'<a href="{escape(item.link, quote=True)}">{title}</a>' if item.link else title]
    for name, value in item.fields.items():
        if name in {"title", "link"} or not value:
            continue
        lines.append(f"{escape(name)}: <b>{escape(clip(value, FIELD_LIMIT))}</b>")
    return lines


def alert_text(watch: Watch, alert: Alert) -> str:
    lines = [f"🔔 <b>{escape(watch.name)}</b> · #{watch.id}", *_item_lines(alert.item)]
    if alert.ai_status is AiStatus.PASSED:
        lines.append(f"<i>AI: {escape(alert.ai_reason)}</i>")
    elif alert.ai_status is AiStatus.UNCHECKED:
        lines.append(f"<i>{escape(alert.ai_reason)}</i>")
    return _fit("\n".join(lines))


def watch_failing_text(watch: Watch, error: str) -> str:
    return (
        f"⚠️ Watch #{watch.id} <b>{escape(watch.name)}</b> failed 3 checks in a row:\n"
        f"<code>{escape(clip(error, 300))}</code>\n"
        "I keep trying less often. /test it, or /unwatch it if the source is gone."
    )


def interval_label(seconds: int) -> str:
    minutes = seconds // 60
    if minutes % 1440 == 0:
        return f"{minutes // 1440} d"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min"


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "not yet"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def watch_line(watch: Watch) -> str:
    state = "⏸" if watch.paused else ("⚠️" if watch.error_count else "▶️")
    return f"{state} #{watch.id} {escape(clip(watch.name, 60))}"


def list_text(watches: list[Watch], limit: int) -> str:
    if not watches:
        return "You have no watches yet. Add one with /watch."
    return (
        f"<b>Your watches</b> ({len(watches)}/{limit})\n"
        + "\n".join(watch_line(w) for w in watches)
        + "\n\nTap one to manage it."
    )


def watch_card(watch: Watch, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    lines = [
        f"<b>#{watch.id} {escape(watch.name)}</b>",
        f"{watch.kind.value.upper()} · every {interval_label(watch.interval_seconds)}"
        + (" · <b>paused</b>" if watch.paused else ""),
        escape(watch.url),
    ]
    if watch.kind is not WatchKind.RSS:
        lines.append(f"items: <code>{escape(watch.spec.items)}</code>")
        lines.extend(
            f"{escape(name)}: <code>{escape(sel)}</code>" for name, sel in watch.spec.fields.items()
        )
    keywords = format_keywords(watch.keywords)
    lines.append(f"keywords: {escape(keywords) if keywords else 'any new item'}")
    if watch.criteria:
        lines.append(f"AI criteria: <i>{escape(watch.criteria)}</i>")
    lines.append(f"last check: {_ago(watch.last_checked_at, now)}")
    if watch.error_count:
        error = escape(clip(watch.last_error, 200))
        lines.append(f"⚠️ {watch.error_count} failed checks: <code>{error}</code>")
    return "\n".join(lines)


def preview_text(result: CheckResult, *, title: str) -> str:
    lines = [
        f"<b>{escape(title)}</b>",
        f"{result.items_found} items found, {len(result.alerts)} match your filters.",
    ]
    if result.warning:
        lines.append(f"⚠️ {escape(result.warning)}")
    for alert in result.alerts:
        lines.append("")
        lines.append("• " + "\n".join(_item_lines(alert.item)))
        if alert.ai_status is not AiStatus.OFF:
            lines.append(f"<i>AI: {escape(alert.ai_reason)}</i>")
    for item, reason in result.rejected:
        lines.append("")
        lines.append(f"✖️ {escape(clip(item.title, 120))}\n<i>AI skipped it: {escape(reason)}</i>")
    return _fit("\n".join(lines))


def stats_text(stats: Stats) -> str:
    return (
        "<b>Bot stats</b>\n"
        f"watches: {stats.watches} ({stats.active_watches} active, "
        f"{stats.failing_watches} failing)\n"
        f"chats: {stats.chats}\n"
        f"seen items stored: {stats.seen_items}"
    )


def _fit(text: str) -> str:
    """Stay under Telegram's limit without cutting through an HTML tag."""
    if len(text) <= MESSAGE_LIMIT:
        return text
    cut = text.rfind("\n", 0, MESSAGE_LIMIT - 20)
    return text[: cut if cut > 0 else MESSAGE_LIMIT - 20] + "\n…"
