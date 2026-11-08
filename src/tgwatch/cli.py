"""Command line: "tgwatch run" starts the bot, "tgwatch check" tries one watch once."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

import anthropic

from tgwatch.ai_filter import ClaudeRelevanceFilter, RelevanceFilter
from tgwatch.config import Settings
from tgwatch.engine import AiStatus, CheckResult, WatchEngine
from tgwatch.errors import WatchError
from tgwatch.fetch import DEFAULT_USER_AGENT, Fetcher
from tgwatch.filters import parse_keywords
from tgwatch.models import ParseSpec, Watch, WatchKind
from tgwatch.store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgwatch", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the Telegram bot and the scheduler")
    run.add_argument("-v", "--verbose", action="store_true")

    check = sub.add_parser("check", help="fetch a source once and print the items that match")
    check.add_argument("url")
    check.add_argument("--kind", choices=[k.value for k in WatchKind], default="rss")
    check.add_argument("--items", default="", help="CSS selector (html) or JSON path (json)")
    check.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="NAME=SELECTOR",
        help="extra field, relative to one item (title=, link=, price=...). Repeatable.",
    )
    check.add_argument("--keywords", default="", help='e.g. "python, remote, -senior"')
    check.add_argument("--criteria", default="", help="plain-language AI filter (needs API key)")
    check.add_argument("--limit", type=int, default=10)
    check.add_argument("-v", "--verbose", action="store_true")
    return parser


def _parse_fields(pairs: Sequence[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for pair in pairs:
        name, sep, selector = pair.partition("=")
        if not sep or not name.strip() or not selector.strip():
            raise SystemExit(f"--field expects NAME=SELECTOR, got {pair!r}")
        fields[name.strip()] = selector.strip()
    return fields


async def _check(args: argparse.Namespace, settings: Settings) -> int:
    watch = Watch(
        id=0,
        chat_id=0,
        name="cli",
        url=args.url,
        kind=WatchKind(args.kind),
        spec=ParseSpec(items=args.items, fields=_parse_fields(args.field)),
        keywords=parse_keywords(args.keywords),
        criteria=args.criteria,
    )
    relevance: RelevanceFilter | None = None
    if args.criteria and settings.anthropic_api_key:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
        relevance = ClaudeRelevanceFilter(client=client, model=settings.anthropic_model)
    elif args.criteria:
        print("note: ANTHROPIC_API_KEY is not set, --criteria is ignored", file=sys.stderr)

    async with (
        Store(":memory:") as store,
        Fetcher(
            user_agent=settings.user_agent or DEFAULT_USER_AGENT,
            timeout=settings.http_timeout_seconds,
            respect_robots=settings.respect_robots_txt,
            allow_private_hosts=settings.allow_private_hosts,
        ) as fetcher,
    ):
        engine = WatchEngine(store, fetcher, relevance)
        try:
            result = await engine.preview(watch, limit=args.limit)
        except WatchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    _print_result(result)
    return 0


def _print_result(result: CheckResult) -> None:
    print(f"{result.items_found} items on the page, {len(result.alerts)} would alert")
    if result.warning:
        print(f"warning: {result.warning}")
    for alert in result.alerts:
        print(f"\n* {alert.item.title}")
        for name, value in alert.item.fields.items():
            print(f"  {name}: {value}")
        if alert.item.link:
            print(f"  {alert.item.link}")
        if alert.ai_status is not AiStatus.OFF:
            print(f"  AI ({alert.ai_status.value}): {alert.ai_reason}")
    for item, reason in result.rejected:
        print(f"\n- skipped by AI: {item.title}\n  {reason}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    if args.command == "run":
        from tgwatch.bot.app import run  # imported here so `check` does not load aiogram

        logging.getLogger().setLevel(logging.DEBUG if args.verbose else logging.INFO)
        asyncio.run(run(settings))
        return 0
    if args.command == "check":
        return asyncio.run(_check(args, settings))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
