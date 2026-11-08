"""Render a chat transcript from the E2E tests as a Telegram-like HTML page.

Usage:
    TGWATCH_TRANSCRIPT=/tmp/chat.json uv run pytest -k one_line_watch
    uv run python scripts/render_transcript.py /tmp/chat.json docs/chat.html

The tests run against a local fake Bot API, so the feed lives on 127.0.0.1 with a random
port. The render shows that address as ``jobs.example.com`` to keep the picture readable.
Bot texts are Telegram HTML produced by the bot itself (already escaped); user texts are
escaped here.
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any

LOCAL_URL = re.compile(r"https?://127\.0\.0\.1:\d+")
LOCAL_HOST = re.compile(r"127\.0\.0\.1(:\d+)?")
SAMPLE_HOST = "jobs.example.com"

CSS = """
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.4 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  background: #0e1621; color: #f5f5f5; }
.wrap { width: 560px; margin: 0 auto; padding: 0 0 18px; background: #0e1621; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  background: #17212b; border-bottom: 1px solid #0b121a; }
.avatar { width: 40px; height: 40px; border-radius: 50%; background: #3e8ed0;
  display: grid; place-items: center; font-weight: 700; font-size: 18px; }
header b { display: block; } header small { color: #7f91a4; }
.chat { padding: 14px 14px 0; display: flex; flex-direction: column; gap: 6px; }
.msg { max-width: 84%; padding: 7px 11px; border-radius: 12px; white-space: pre-wrap;
  word-wrap: break-word; }
.bot { align-self: flex-start; background: #182533; border-bottom-left-radius: 4px; }
.user { align-self: flex-end; background: #2b5278; border-bottom-right-radius: 4px; }
.msg a { color: #71baf2; text-decoration: none; }
.kb { align-self: flex-start; width: 84%; display: grid; gap: 4px; margin-top: -2px; }
.kb span { display: block; text-align: center; padding: 7px; border-radius: 8px;
  background: rgba(62, 84, 107, .55); font-size: 14px; }
.kb.pressed span { outline: 2px solid #71baf2; }
.note { align-self: center; color: #9fb2c4; background: rgba(0,0,0,.25); font-size: 13px;
  padding: 3px 10px; border-radius: 10px; margin: 4px 0; }
footer { color: #7f91a4; font-size: 12px; text-align: center; padding: 12px 16px 0; }
"""


def clean(text: str) -> str:
    text = LOCAL_URL.sub(f"https://{SAMPLE_HOST}", text)
    return LOCAL_HOST.sub(SAMPLE_HOST, text)


def keyboard(rows: list[list[str]], pressed: str | None) -> str:
    cls = "kb pressed" if pressed and any(r[0] == pressed for r in rows) else "kb"
    cells = "".join(f"<span>{html.escape(clean(row[0]))}</span>" for row in rows)
    return f'<div class="{cls}">{cells}</div>'


def render(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for i, event in enumerate(events):
        if event["from"] == "user":
            if "press" in event:
                parts.append(f'<div class="note">tapped “{html.escape(event["press"])}”</div>')
            else:
                parts.append(f'<div class="msg user">{html.escape(clean(event["text"]))}</div>')
            continue
        if event.get("method") == "editMessageReplyMarkup":
            parts.append('<div class="note">watch paused, button removed</div>')
            continue
        parts.append(f'<div class="msg bot">{clean(event["text"])}</div>')
        if event.get("buttons"):
            nxt = events[i + 1] if i + 1 < len(events) else {}
            parts.append(keyboard(event["buttons"], nxt.get("press")))
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>telegram-watch-bot chat</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>"
        "<header><div class='avatar'>W</div><div><b>Watch bot (test run)</b>"
        "<small>local fake Bot API · sample feed</small></div></header>"
        f"<div class='chat'>{''.join(parts)}</div>"
        "<footer>Recorded from tests/test_bot_e2e.py. The test feed is sample data; its local "
        f"address is shown as {SAMPLE_HOST}.</footer></div></body></html>"
    )


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: render_transcript.py <transcript.json> <out.html>")
    events = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    Path(sys.argv[2]).write_text(render(events), encoding="utf-8")


if __name__ == "__main__":
    main()
