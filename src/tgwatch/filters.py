"""Keyword rules typed by users: ``python, remote, -senior, "full stack", react*``."""

from __future__ import annotations

import re
import shlex
from functools import lru_cache

from tgwatch.models import Item, KeywordRule

MAX_TERMS = 30
MAX_TERM_LENGTH = 60


def parse_keywords(text: str) -> KeywordRule:
    """Split user input into include/exclude terms.

    Terms are separated by commas, or by spaces when there is no comma. Quotes keep a phrase
    together, a leading ``-`` excludes, a trailing ``*`` matches any word ending.
    """
    text = text.strip()
    if not text:
        return KeywordRule()
    if "," in text:
        raw = [part.strip().strip("\"'") for part in text.split(",")]
    else:
        try:
            raw = shlex.split(text)
        except ValueError:  # unbalanced quote: fall back to plain whitespace split
            raw = text.replace('"', " ").split()
    include: list[str] = []
    exclude: list[str] = []
    for term in raw:
        target = include
        if term.startswith("-") and len(term) > 1:
            target, term = exclude, term[1:].strip()
        term = " ".join(term.casefold().split())[:MAX_TERM_LENGTH]
        if term and term != "*" and term not in target:
            target.append(term)
    return KeywordRule(include=tuple(include[:MAX_TERMS]), exclude=tuple(exclude[:MAX_TERMS]))


def format_keywords(rule: KeywordRule) -> str:
    def show(term: str) -> str:
        return f'"{term}"' if " " in term else term

    parts = [show(t) for t in rule.include] + [f"-{show(t)}" for t in rule.exclude]
    return ", ".join(parts)


def matches(rule: KeywordRule, item: Item) -> bool:
    text = item.searchable_text().casefold()
    if any(_pattern(term).search(text) for term in rule.exclude):
        return False
    return not rule.include or any(_pattern(term).search(text) for term in rule.include)


@lru_cache(maxsize=1024)
def _pattern(term: str) -> re.Pattern[str]:
    # Word boundaries so "java" does not match "javascript"; "java*" opts into prefix matching.
    prefix = term.endswith("*")
    body = r"\s+".join(re.escape(word) for word in term.rstrip("*").split())
    tail = r"\w*" if prefix else r"(?!\w)"
    return re.compile(rf"(?<!\w){body}{tail}")
