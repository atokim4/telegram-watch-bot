"""Polite HTTP fetching: timeouts, retries with backoff, per-host pacing, robots.txt, size cap."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from tgwatch import __version__
from tgwatch.errors import FetchError, RobotsDisallowedError

log = logging.getLogger(__name__)

ROBOTS_AGENT = "tgwatch"
DEFAULT_USER_AGENT = (
    f"{ROBOTS_AGENT}/{__version__} (+https://github.com/atokim4/telegram-watch-bot)"
)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 120.0
ROBOTS_TTL_SECONDS = 3600.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    content: bytes
    encoding: str | None
    etag: str = ""
    last_modified: str = ""

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


@dataclass(slots=True)
class _HostState:
    lock: asyncio.Lock
    last_request: float = float("-inf")


@dataclass(slots=True)
class _RobotsEntry:
    parser: RobotFileParser | None
    """None means "allow everything" (robots.txt missing or a 4xx)."""
    fetched_at: float


async def _ensure_public_host(host: str) -> None:
    """Refuse hosts that resolve to loopback, private, link-local or other non-global ranges."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve host {host!r}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise FetchError(f"host {host!r} resolves to a non-public address")


class Fetcher:
    """One shared instance per process; it keeps the connection pool and per-host state."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        host_min_interval: float = 2.0,
        max_bytes: int = 5 * 1024 * 1024,
        respect_robots: bool = True,
        allow_private_hosts: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._user_agent = user_agent
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._host_min_interval = host_min_interval
        self._max_bytes = max_bytes
        self._respect_robots = respect_robots
        self._allow_private_hosts = allow_private_hosts
        self._sleep = sleep
        self._hosts: dict[str, _HostState] = {}
        self._robots: dict[str, _RobotsEntry] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            follow_redirects=True,
            max_redirects=5,
            transport=transport,
            event_hooks={"request": [self._check_request_host]},
        )

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        url: str,
        *,
        accept: str = "*/*",
        etag: str = "",
        last_modified: str = "",
    ) -> FetchResult:
        """GET ``url``. Returns status 304 with empty content when the validators still match."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise FetchError("only http:// and https:// URLs can be watched")
        if self._respect_robots and not await self._robots_allows(url):
            raise RobotsDisallowedError(f"robots.txt of {parts.hostname} disallows this URL")

        headers = {"Accept": accept}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        response = await self._get_with_retries(url, headers)
        if response.status == 304 or 200 <= response.status < 300:
            return response
        raise FetchError(f"{parts.hostname} answered HTTP {response.status}")

    async def _get_with_retries(self, url: str, headers: dict[str, str]) -> FetchResult:
        attempt = 0
        while True:
            try:
                result, retry_after = await self._get_once(url, headers)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise FetchError(f"network error: {type(exc).__name__}") from exc
                delay = self._backoff(attempt)
            else:
                if result.status not in RETRY_STATUSES or attempt >= self._max_retries:
                    return result
                delay = max(self._backoff(attempt), retry_after or 0.0)
            attempt += 1
            log.info("retrying %s in %.1fs (attempt %d)", url, delay, attempt)
            await self._sleep(delay)

    async def _get_once(
        self, url: str, headers: dict[str, str]
    ) -> tuple[FetchResult, float | None]:
        host = urlsplit(url).hostname or ""
        await self._wait_for_host_slot(host)
        async with self._client.stream("GET", url, headers=headers) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > self._max_bytes:
                    raise FetchError(f"response is larger than {self._max_bytes // 1024} KiB")
            result = FetchResult(
                url=str(response.url),
                status=response.status_code,
                content=bytes(content),
                encoding=response.charset_encoding,
                etag=response.headers.get("ETag", ""),
                last_modified=response.headers.get("Last-Modified", ""),
            )
            return result, _parse_retry_after(response.headers.get("Retry-After"))

    def _backoff(self, attempt: int) -> float:
        # Jitter spreads retries from many watches on the same host; not security relevant.
        return float(self._backoff_base * 2**attempt * (1 + random.random() / 2))  # noqa: S311

    async def _wait_for_host_slot(self, host: str) -> None:
        state = self._hosts.setdefault(host, _HostState(lock=asyncio.Lock()))
        async with state.lock:
            wait = state.last_request + self._host_min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait)
            state.last_request = time.monotonic()

    async def _check_request_host(self, request: httpx.Request) -> None:
        # Runs for the first request and for every redirect hop.
        if not self._allow_private_hosts:
            await _ensure_public_host(request.url.host)

    async def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        # One lock per origin: a slow robots.txt on one site must not hold up the others.
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            entry = self._robots.get(origin)
            if entry is None or time.monotonic() - entry.fetched_at > ROBOTS_TTL_SECONDS:
                entry = await self._load_robots(origin)
                self._robots[origin] = entry
        return entry.parser is None or entry.parser.can_fetch(ROBOTS_AGENT, url)

    async def _load_robots(self, origin: str) -> _RobotsEntry:
        # RFC 9309: 4xx means no restrictions, 5xx or unreachable means assume full disallow.
        now = time.monotonic()
        try:
            result, _ = await self._get_once(f"{origin}/robots.txt", {"Accept": "text/plain"})
        except (httpx.TransportError, FetchError) as exc:
            raise FetchError(f"robots.txt of {origin} is unreachable, not fetching") from exc
        if 400 <= result.status < 500:
            return _RobotsEntry(parser=None, fetched_at=now)
        if result.status >= 500:
            raise FetchError(f"robots.txt of {origin} answered HTTP {result.status}, not fetching")
        parser = RobotFileParser()
        parser.parse(result.text.splitlines())
        return _RobotsEntry(parser=parser, fetched_at=now)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)
