"""Turn fetched RSS/Atom, HTML or JSON into a list of :class:`Item`."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

import feedparser
from selectolax.lexbor import LexborHTMLParser, LexborNode, SelectolaxError

from tgwatch.errors import ParseError
from tgwatch.jsonpath import compile_path
from tgwatch.models import Item, ParseSpec, WatchKind

SUMMARY_LIMIT = 500
FIELD_LIMIT = 200
_SPACES = re.compile(r"\s+")

JSON_TITLE_KEYS = ("title", "name", "headline", "subject")
JSON_LINK_KEYS = ("html_url", "url", "link", "permalink", "href")
JSON_ID_KEYS = ("id", "guid", "uuid", "key")


def parse(kind: WatchKind, content: bytes, base_url: str, spec: ParseSpec) -> list[Item]:
    match kind:
        case WatchKind.RSS:
            items = parse_feed(content, base_url, spec)
        case WatchKind.HTML:
            items = parse_html(content, base_url, spec)
        case WatchKind.JSON:
            items = parse_json(content, base_url, spec)
    return list(_unique(items))


def parse_feed(content: bytes, base_url: str, spec: ParseSpec) -> list[Item]:
    # Always bytes: given a str, feedparser would treat it as a URL or path and open it itself.
    feed = feedparser.parse(content, response_headers={"content-location": base_url})
    entries: list[Any] = list(feed.entries)
    if not entries and feed.bozo:
        raise ParseError(f"not a valid RSS/Atom feed ({type(feed.bozo_exception).__name__})")
    items: list[Item] = []
    for entry in entries:
        title = _clean(entry.get("title", ""))
        link = urljoin(base_url, entry.get("link", "")) if entry.get("link") else ""
        uid = entry.get("id") or link or title
        if not uid:
            continue
        summary = _html_to_text(entry.get("summary", ""))
        fields = {
            name: _clip(_clean(str(entry.get(key, ""))), FIELD_LIMIT)
            for name, key in spec.fields.items()
            if entry.get(key)
        }
        items.append(
            Item(uid=str(uid), title=title or link, link=link, summary=summary, fields=fields)
        )
    return items


def parse_html(content: bytes, base_url: str, spec: ParseSpec) -> list[Item]:
    if not spec.items:
        raise ParseError("an HTML watch needs a CSS selector for the items")
    tree = LexborHTMLParser(content)
    try:
        nodes = tree.css(spec.items)
    except (SelectolaxError, ValueError) as exc:
        raise ParseError(f"invalid CSS selector {spec.items!r}") from exc

    extra = {name: sel for name, sel in spec.fields.items() if name not in {"title", "link"}}
    items: list[Item] = []
    for node in nodes:
        title_node = _select_one(node, spec.fields.get("title"))
        title = _node_text(title_node) if title_node is not None else _node_text(node)
        link = _node_link(node, spec.fields.get("link"), base_url)
        if not title and not link:
            continue
        fields: dict[str, str] = {}
        for name, selector in extra.items():
            found = _select_one(node, selector)
            if found is not None and (text := _node_text(found)):
                fields[name] = _clip(text, FIELD_LIMIT)
        uid = link or title
        items.append(
            Item(uid=uid, title=_clip(title or link, FIELD_LIMIT), link=link, fields=fields)
        )
    return items


def parse_json(content: bytes, base_url: str, spec: ParseSpec) -> list[Item]:
    try:
        document = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError("response is not valid JSON") from exc
    path = compile_path(spec.items)
    found = path.find(document)
    records: list[Any] = found[0] if len(found) == 1 and isinstance(found[0], list) else found
    field_paths = {name: compile_path(p) for name, p in spec.fields.items()}

    items: list[Item] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        values = {name: _scalar(p.first(record)) for name, p in field_paths.items()}
        title = values.pop("title", "") or _first_key(record, JSON_TITLE_KEYS)
        raw_link = values.pop("link", "") or _first_key(record, JSON_LINK_KEYS)
        link = urljoin(base_url, raw_link) if raw_link else ""
        uid = values.pop("id", "") or _first_key(record, JSON_ID_KEYS) or link or title
        if not uid:
            continue
        fields = {k: _clip(v, FIELD_LIMIT) for k, v in values.items() if v}
        items.append(
            Item(uid=uid, title=_clip(title or link or uid, FIELD_LIMIT), link=link, fields=fields)
        )
    return items


def _unique(items: Iterable[Item]) -> Iterable[Item]:
    seen: set[str] = set()
    for item in items:
        if item.key not in seen:
            seen.add(item.key)
            yield item


def _select_one(node: LexborNode, selector: str | None) -> LexborNode | None:
    if not selector:
        return None
    try:
        return node.css_first(selector)
    except (SelectolaxError, ValueError) as exc:
        raise ParseError(f"invalid CSS selector {selector!r}") from exc


def _node_link(node: LexborNode, selector: str | None, base_url: str) -> str:
    if selector:
        target = _select_one(node, selector)
    elif node.tag == "a":
        target = node
    else:
        target = node.css_first("a[href]")
    href = target.attributes.get("href") if target is not None else None
    if not href or href.startswith(("javascript:", "#")):
        return ""
    return urljoin(base_url, href)


def _node_text(node: LexborNode) -> str:
    return _clean(node.text(separator=" ", strip=True))


def _html_to_text(markup: str) -> str:
    if not markup:
        return ""
    if "<" not in markup:
        return _clip(_clean(markup), SUMMARY_LIMIT)
    body = LexborHTMLParser(markup).body
    text = body.text(separator=" ", strip=True) if body is not None else ""
    return _clip(_clean(text), SUMMARY_LIMIT)


def _first_key(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if value := _scalar(record.get(key)):
            return value
    return ""


def _scalar(value: Any) -> str:
    if value is None or isinstance(value, dict | list):
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _clean(str(value))


def _clean(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
