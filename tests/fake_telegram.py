"""A local stand-in for the Telegram Bot API, so the bot can be tested without a real token.

It serves ``/bot<token>/<method>`` like api.telegram.org: ``getUpdates`` hands out updates the
test queued, and every outgoing call (``sendMessage``, ``editMessageText``...) is recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

BOT_USER = {"id": 4242, "is_bot": True, "first_name": "Watch bot", "username": "sample_watch_bot"}
USER = {"id": 1001, "is_bot": False, "first_name": "Sample", "username": "sample_user"}
VISIBLE_METHODS = {"sendMessage", "editMessageText", "editMessageReplyMarkup"}


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    error: int = 0
    """HTTP error the stub answered with (0 = delivered)."""

    @property
    def text(self) -> str:
        return str(self.params.get("text", ""))

    @property
    def chat_id(self) -> int:
        return int(self.params["chat_id"])

    @property
    def message_id(self) -> int:
        return int(self.params.get("message_id", 0))

    @property
    def buttons(self) -> list[tuple[str, str]]:
        markup = self.params.get("reply_markup") or {}
        return [
            (button["text"], button.get("callback_data", ""))
            for row in markup.get("inline_keyboard", [])
            for button in row
        ]


@dataclass
class FakeTelegram:
    base_url: str = ""
    calls: list[Call] = field(default_factory=list)
    blocked_chats: set[int] = field(default_factory=set)
    flood_next: int = 0
    """Answer the next sendMessage with 429 and this retry_after (seconds), once."""
    messages: dict[int, dict[str, Any]] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    """The chat as a person would see it: user texts, button presses and bot output, in order."""
    _updates: list[dict[str, Any]] = field(default_factory=list)
    activity: asyncio.Event = field(default_factory=asyncio.Event)
    """Set whenever the bot sends or edits a message."""
    _new_update: asyncio.Event = field(default_factory=asyncio.Event)
    _update_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _message_ids: itertools.count[int] = field(default_factory=lambda: itertools.count(100))

    # --- what the test does -------------------------------------------------------------

    def push_text(
        self, text: str, *, chat_id: int = USER["id"], user_id: int | None = None
    ) -> None:
        user = {**USER, "id": user_id or chat_id}
        message: dict[str, Any] = {
            "message_id": next(self._message_ids),
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private", "first_name": user["first_name"]},
            "from": user,
            "text": text,
        }
        if text.startswith("/"):
            command = text.split()[0]
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        self.transcript.append({"from": "user", "text": text})
        self._push({"message": message})

    def push_callback(self, message_id: int, data: str) -> None:
        message = self.messages[message_id]
        label = next(
            (
                button["text"]
                for row in message.get("reply_markup", {}).get("inline_keyboard", [])
                for button in row
                if button.get("callback_data") == data
            ),
            data,
        )
        self.transcript.append({"from": "user", "press": label, "message_id": message_id})
        self._push(
            {
                "callback_query": {
                    "id": str(next(self._message_ids)),
                    "from": {**USER, "id": message["chat"]["id"]},
                    "chat_instance": "1",
                    "message": message,
                    "data": data,
                }
            }
        )

    def visible(self) -> list[Call]:
        return [c for c in self.calls if c.method in VISIBLE_METHODS and not c.error]

    def answers(self) -> list[Call]:
        return [call for call in self.calls if call.method == "answerCallbackQuery"]

    def _push(self, update: dict[str, Any]) -> None:
        self._updates.append({"update_id": next(self._update_ids), **update})
        self._new_update.set()

    # --- the Bot API side ---------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = {key: _decode(value) for key, value in (await request.post()).items()}
        if method == "getUpdates":
            return _ok(await self._get_updates(params))
        call = Call(method, params)
        self.calls.append(call)
        response = self._dispatch(call)
        if method in VISIBLE_METHODS and not call.error:
            self.activity.set()
            self.transcript.append(
                {
                    "from": "bot",
                    "method": method,
                    "text": call.text,
                    "buttons": call.buttons,
                    "message_id": call.message_id,
                }
            )
        return response

    def _dispatch(self, call: Call) -> web.Response:
        method, params = call.method, call.params
        if method == "getMe":
            return _ok(BOT_USER)
        if method == "sendMessage":
            return self._send_message(call)
        if method in {"editMessageText", "editMessageReplyMarkup"}:
            return self._edit_message(method, params)
        return _ok(True)

    async def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        offset = int(params.get("offset", 0))
        timeout = min(float(params.get("timeout", 0)), 0.5)
        pending = [u for u in self._updates if u["update_id"] >= offset]
        if not pending:
            self._new_update.clear()
            try:
                await asyncio.wait_for(self._new_update.wait(), timeout=timeout)
            except TimeoutError:
                return []
            pending = [u for u in self._updates if u["update_id"] >= offset]
        return pending

    def _send_message(self, call: Call) -> web.Response:
        params = call.params
        chat_id = int(params["chat_id"])
        if chat_id in self.blocked_chats:
            call.error = 403
            return _error(403, "Forbidden: bot was blocked by the user")
        if self.flood_next:
            retry, self.flood_next = self.flood_next, 0
            call.error = 429
            return _error(429, f"Too Many Requests: retry after {retry}", retry_after=retry)
        message = {
            "message_id": next(self._message_ids),
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": BOT_USER,
            "text": params.get("text", ""),
        }
        if params.get("reply_markup"):
            message["reply_markup"] = params["reply_markup"]
        self.messages[message["message_id"]] = message
        params["message_id"] = message["message_id"]
        return _ok(message)

    def _edit_message(self, method: str, params: dict[str, Any]) -> web.Response:
        message = self.messages.get(int(params["message_id"]))
        if message is None:
            return _error(400, "Bad Request: message to edit not found")
        if method == "editMessageText":
            message["text"] = params.get("text", "")
        if params.get("reply_markup"):
            message["reply_markup"] = params["reply_markup"]
        else:
            message.pop("reply_markup", None)
        return _ok(message)


def _decode(value: Any) -> Any:
    # aiogram sends nested objects (reply_markup, ...) as JSON strings inside the form.
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


def _error(code: int, description: str, **parameters: Any) -> web.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return web.json_response(body, status=code)


FAKE_KEY = web.AppKey("fake", FakeTelegram)


async def _route(request: web.Request) -> web.Response:
    return await request.app[FAKE_KEY].handle(request)


@contextlib.asynccontextmanager
async def serve_fake_telegram() -> AsyncIterator[FakeTelegram]:
    fake = FakeTelegram()
    app = web.Application()
    app[FAKE_KEY] = fake
    app.router.add_post("/bot{token}/{method}", _route)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        yield fake
    finally:
        await runner.cleanup()
