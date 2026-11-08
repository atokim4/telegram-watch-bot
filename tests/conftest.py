"""Shared fixtures: a local HTTP site whose content tests can change between polls."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web

from tgwatch.fetch import Fetcher
from tgwatch.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass
class Page:
    body: bytes
    content_type: str = "text/html"
    status: int = 200
    etag: str = ""
    fail_times: int = 0
    """Answer 503 this many times before serving the page."""
    retry_after: str = ""


@dataclass
class Site:
    base_url: str = ""
    pages: dict[str, Page] = field(default_factory=dict)
    robots: str | None = None
    robots_status: int = 404
    """Status served for robots.txt while ``robots`` is None."""
    hits: dict[str, int] = field(default_factory=dict)
    user_agents: list[str] = field(default_factory=list)

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def set(
        self, path: str, body: bytes | str, content_type: str = "text/html", **kw: object
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.pages[path] = Page(body=data, content_type=content_type, **kw)  # type: ignore[arg-type]


async def _handle(request: web.Request) -> web.Response:
    site = request.app[SITE_KEY]
    path = request.path_qs
    site.hits[path] = site.hits.get(path, 0) + 1
    site.user_agents.append(request.headers.get("User-Agent", ""))
    if request.path == "/robots.txt":
        if site.robots is None:
            return web.Response(status=site.robots_status)
        return web.Response(text=site.robots, content_type="text/plain")
    page = site.pages.get(path)
    if page is None:
        return web.Response(status=404, text="not found")
    if page.fail_times > 0:
        page.fail_times -= 1
        headers = {"Retry-After": page.retry_after} if page.retry_after else {}
        return web.Response(status=503, text="busy", headers=headers)
    if page.etag and request.headers.get("If-None-Match") == page.etag:
        return web.Response(status=304, headers={"ETag": page.etag})
    headers = {"ETag": page.etag} if page.etag else {}
    return web.Response(
        body=page.body, status=page.status, content_type=page.content_type, headers=headers
    )


SITE_KEY = web.AppKey("site", Site)


@pytest.fixture
async def site() -> AsyncIterator[Site]:
    state = Site()
    app = web.Application()
    app[SITE_KEY] = state
    app.router.add_route("GET", "/{tail:.*}", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    server = web.TCPSite(runner, "127.0.0.1", 0)
    await server.start()
    port = runner.addresses[0][1]
    state.base_url = f"http://127.0.0.1:{port}"
    yield state
    await runner.cleanup()


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def fetcher(sleeps: RecordingSleep) -> AsyncIterator[Fetcher]:
    async with Fetcher(
        allow_private_hosts=True, host_min_interval=0, backoff_base=0.01, sleep=sleeps
    ) as instance:
        yield instance


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    async with Store(tmp_path / "test.sqlite3") as instance:
        yield instance
