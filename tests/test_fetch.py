import time

import httpx
import pytest
from conftest import RecordingSleep, Site

from tgwatch.errors import FetchError, RobotsDisallowedError
from tgwatch.fetch import DEFAULT_USER_AGENT, Fetcher


async def test_fetch_sends_honest_user_agent(site: Site, fetcher: Fetcher) -> None:
    site.set("/p", "hello")
    result = await fetcher.fetch(site.url("/p"))
    assert result.status == 200
    assert result.text == "hello"
    assert site.user_agents[-1] == DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.startswith("tgwatch/")


async def test_retries_503_then_succeeds_honouring_retry_after(
    site: Site, fetcher: Fetcher, sleeps: RecordingSleep
) -> None:
    site.set("/flaky", "ok", fail_times=2, retry_after="7")
    result = await fetcher.fetch(site.url("/flaky"))
    assert result.text == "ok"
    assert site.hits["/flaky"] == 3
    assert sleeps.calls == [7.0, 7.0]


async def test_gives_up_after_max_retries(site: Site, fetcher: Fetcher) -> None:
    site.set("/down", "x", fail_times=10)
    with pytest.raises(FetchError, match="HTTP 503"):
        await fetcher.fetch(site.url("/down"))
    assert site.hits["/down"] == 4  # first try + 3 retries


async def test_client_errors_are_not_retried(site: Site, fetcher: Fetcher) -> None:
    with pytest.raises(FetchError, match="HTTP 404"):
        await fetcher.fetch(site.url("/missing"))
    assert site.hits["/missing"] == 1


async def test_network_error_is_retried_then_reported(sleeps: RecordingSleep) -> None:
    async with Fetcher(
        allow_private_hosts=True,
        respect_robots=False,
        backoff_base=0.01,
        max_retries=2,
        host_min_interval=0,
        sleep=sleeps,
    ) as fetcher:
        with pytest.raises(FetchError, match="network error"):
            await fetcher.fetch("http://127.0.0.1:9/closed-port")
    assert len(sleeps.calls) == 2
    assert sleeps.calls[1] > sleeps.calls[0]  # exponential backoff


async def test_conditional_get_returns_not_modified(site: Site, fetcher: Fetcher) -> None:
    site.set("/feed", "<rss/>", etag='"v1"')
    first = await fetcher.fetch(site.url("/feed"))
    assert first.etag == '"v1"'
    second = await fetcher.fetch(site.url("/feed"), etag=first.etag)
    assert second.not_modified
    assert second.content == b""


async def test_robots_txt_is_respected_and_cached(site: Site, fetcher: Fetcher) -> None:
    site.robots = "User-agent: tgwatch\nDisallow: /private\n\nUser-agent: *\nAllow: /\n"
    site.set("/public", "ok")
    site.set("/private/x", "secret")
    assert (await fetcher.fetch(site.url("/public"))).text == "ok"
    with pytest.raises(RobotsDisallowedError):
        await fetcher.fetch(site.url("/private/x"))
    assert site.hits["/robots.txt"] == 1
    assert "/private/x" not in site.hits


async def test_robots_server_error_means_disallow(site: Site, fetcher: Fetcher) -> None:
    site.robots_status = 503
    site.set("/feed", "ok")
    with pytest.raises(FetchError, match=r"robots\.txt .* HTTP 503"):
        await fetcher.fetch(site.url("/feed"))
    assert "/feed" not in site.hits


async def test_robots_unreachable_means_disallow(fetcher: Fetcher) -> None:
    with pytest.raises(FetchError, match=r"robots\.txt .* unreachable"):
        await fetcher.fetch("http://127.0.0.1:9/feed")


async def test_rejects_non_http_and_private_hosts() -> None:
    async with Fetcher(respect_robots=False) as fetcher:
        with pytest.raises(FetchError, match="only http"):
            await fetcher.fetch("file:///etc/passwd")
        with pytest.raises(FetchError, match="non-public address"):
            await fetcher.fetch("http://127.0.0.1/")
        with pytest.raises(FetchError, match="non-public address"):
            await fetcher.fetch("http://169.254.169.254/latest/meta-data/")


async def test_redirect_to_private_host_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []

    async def fake_guard(host: str) -> None:
        checked.append(host)
        if host == "10.0.0.5":
            raise FetchError(f"host {host!r} resolves to a non-public address")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "http://10.0.0.5/admin"})
        return httpx.Response(200, text="internal")

    monkeypatch.setattr("tgwatch.fetch._ensure_public_host", fake_guard)
    async with Fetcher(respect_robots=False, transport=httpx.MockTransport(handler)) as fetcher:
        with pytest.raises(FetchError, match="non-public"):
            await fetcher.fetch("https://public.example/start")
    assert checked == ["public.example", "10.0.0.5"]


async def test_size_cap(site: Site) -> None:
    site.set("/big", b"x" * 5000)
    async with Fetcher(allow_private_hosts=True, max_bytes=1024, host_min_interval=0) as fetcher:
        with pytest.raises(FetchError, match="larger than"):
            await fetcher.fetch(site.url("/big"))


async def test_per_host_pacing(site: Site) -> None:
    site.set("/a", "a")
    async with Fetcher(allow_private_hosts=True, host_min_interval=0.2) as fetcher:
        started = time.monotonic()
        for _ in range(3):
            await fetcher.fetch(site.url("/a"))
        elapsed = time.monotonic() - started
    # robots.txt + 3 pages = 4 requests, so at least 3 gaps of 0.2 s.
    assert elapsed >= 0.6
