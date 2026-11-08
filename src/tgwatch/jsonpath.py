"""A small JSONPath subset: ``$.a.b[0].c[*]["key with spaces"]``.

Enough to point at the list of items in a typical JSON API response without pulling in a full
JSONPath engine. Supported steps: ``.key``, ``[n]`` (negative allowed), ``[*]`` (list items or
dict values) and ``["key"]`` / ``['key']``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from tgwatch.errors import ParseError

_TOKEN = re.compile(
    r"""
    \.?(?P<key>[A-Za-z_$@][\w$@-]*)     # .key or a bare first key
    | \[(?P<index>-?\d+)\]               # [0]
    | \[(?P<star>\*)\]                   # [*]
    | \.(?P<dotstar>\*)                  # .*
    | \[(?P<q>["'])(?P<qkey>.*?)(?P=q)\] # ["key"]
    """,
    re.VERBOSE,
)

_WILDCARD = object()


@dataclass(frozen=True, slots=True)
class JsonPath:
    source: str
    steps: tuple[str | int | object, ...]

    @property
    def has_wildcard(self) -> bool:
        return any(step is _WILDCARD for step in self.steps)

    def find(self, document: Any) -> list[Any]:
        """All values the path reaches; missing keys simply produce no match."""
        current: list[Any] = [document]
        for step in self.steps:
            following: list[Any] = []
            for value in current:
                if step is _WILDCARD:
                    if isinstance(value, list):
                        following.extend(value)
                    elif isinstance(value, dict):
                        following.extend(value.values())
                elif isinstance(step, int):
                    if isinstance(value, list) and -len(value) <= step < len(value):
                        following.append(value[step])
                elif isinstance(value, dict) and step in value:
                    following.append(value[step])
            current = following
        return current

    def first(self, document: Any) -> Any:
        found = self.find(document)
        return found[0] if found else None


def compile_path(path: str) -> JsonPath:
    text = path.strip()
    if text in {"", "$"}:
        return JsonPath(source=path, steps=())
    if text.startswith("$"):
        text = text[1:]
    steps: list[str | int | object] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ParseError(f"invalid JSON path {path!r} near {text[position:]!r}")
        if match["key"] is not None:
            steps.append(match["key"])
        elif match["index"] is not None:
            steps.append(int(match["index"]))
        elif match["star"] or match["dotstar"]:
            steps.append(_WILDCARD)
        else:
            steps.append(match["qkey"])
        position = match.end()
    return JsonPath(source=path, steps=tuple(steps))
