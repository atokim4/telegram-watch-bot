"""Bot pieces without Telegram: input validation, limits and message formatting."""

import pytest

from tgwatch.bot import texts
from tgwatch.bot.service import (
    PreviewLimiter,
    UserError,
    parse_field_lines,
    validate_url,
    watch_name,
)
from tgwatch.engine import AiStatus, Alert, CheckResult
from tgwatch.models import Item, Watch, WatchKind


def test_validate_url() -> None:
    assert validate_url("  https://example.com/feed  ") == "https://example.com/feed"
    for bad in ("", "example.com", "javascript:alert(1)", "file:///etc/passwd", "https://a b"):
        with pytest.raises(UserError):
            validate_url(bad)
    with pytest.raises(UserError):
        validate_url("https://example.com/" + "a" * 3000)


def test_parse_field_lines_keeps_commas_in_selectors() -> None:
    assert parse_field_lines("title = h2 a, h3 a\nPrice=.price; link=a.more") == {
        "title": "h2 a, h3 a",
        "price": ".price",
        "link": "a.more",
    }
    with pytest.raises(UserError, match="name=selector"):
        parse_field_lines("just a selector")
    with pytest.raises(UserError, match="Field names"):
        parse_field_lines("my price!=.p")
    with pytest.raises(UserError, match="At most"):
        parse_field_lines("\n".join(f"f{i}=.x" for i in range(9)))


def test_watch_name() -> None:
    assert watch_name("https://www.example.com/jobs/?q=1") == "example.com/jobs"
    assert len(watch_name("https://example.com/" + "x" * 100)) == 60


def test_preview_limiter() -> None:
    now = [100.0]
    limiter = PreviewLimiter(cooldown=10, clock=lambda: now[0])
    limiter.acquire(1)
    limiter.acquire(2)  # other chats are not affected
    with pytest.raises(UserError, match="Try again in 10 s"):
        limiter.acquire(1)
    now[0] += 10
    limiter.acquire(1)


def _watch(**kw: object) -> Watch:
    return Watch(
        id=7,
        chat_id=1,
        name="shop.example/<b>",
        url="https://shop.example",
        kind=WatchKind.HTML,
        **kw,
    )  # type: ignore[arg-type]


def test_alert_text_escapes_site_content() -> None:
    item = Item(
        uid="1",
        title="<script>alert(1)</script> & co",
        link='https://shop.example/a?x=1&y="2"',
        fields={"price": "<b>9</b>", "title": "ignored"},
    )
    text = texts.alert_text(_watch(), Alert(item, AiStatus.PASSED, "fits <budget>"))
    assert "<script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in text
    assert 'href="https://shop.example/a?x=1&amp;y=&quot;2&quot;"' in text
    assert "price: <b>&lt;b&gt;9&lt;/b&gt;</b>" in text
    assert "ignored" not in text
    assert "<i>AI: fits &lt;budget&gt;</i>" in text
    assert "shop.example/&lt;b&gt;" in text


def test_preview_text_stays_under_telegram_limit() -> None:
    alerts = [Alert(Item(uid=str(i), title="x" * 290, link=f"https://e.x/{i}")) for i in range(40)]
    result = CheckResult(watch_id=1, items_found=40, alerts=alerts)
    text = texts.preview_text(result, title="Dry run")
    assert len(text) <= texts.MESSAGE_LIMIT
    assert text.endswith("…")
    assert text.count("<a ") == text.count("</a>")


def test_watch_card_shows_errors_and_selectors() -> None:
    watch = _watch(error_count=2, last_error="HTTP 503 <busy>")
    card = texts.watch_card(watch)
    assert "HTML · every 5 min" in card
    assert "2 failed checks" in card
    assert "&lt;busy&gt;" in card
