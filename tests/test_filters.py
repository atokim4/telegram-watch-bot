import pytest

from tgwatch.filters import format_keywords, matches, parse_keywords
from tgwatch.models import Item, KeywordRule


def item(title: str, summary: str = "", **fields: str) -> Item:
    return Item(uid=title, title=title, summary=summary, fields=fields)


def test_parse_commas_quotes_and_excludes() -> None:
    rule = parse_keywords('Python, "Full  Stack", -Senior, react*, python')
    assert rule.include == ("python", "full stack", "react*")
    assert rule.exclude == ("senior",)


def test_parse_space_separated_with_quotes() -> None:
    rule = parse_keywords('python "telegram bot" -crypto')
    assert rule == KeywordRule(include=("python", "telegram bot"), exclude=("crypto",))


def test_parse_tolerates_unbalanced_quote_and_empty() -> None:
    assert parse_keywords('python "bot').include == ("python", "bot")
    assert parse_keywords("   ") == KeywordRule()
    assert parse_keywords("-, *") == KeywordRule(include=("-",))


def test_format_round_trip() -> None:
    rule = parse_keywords('python, "full stack", -senior')
    assert format_keywords(rule) == 'python, "full stack", -senior'
    assert parse_keywords(format_keywords(rule)) == rule


@pytest.mark.parametrize(
    ("keywords", "title", "expected"),
    [
        ("", "anything", True),
        ("java", "JavaScript widget", False),
        ("java*", "JavaScript widget", True),
        ("java", "Senior Java engineer", True),
        ("full stack", "Full\nstack dev", True),
        ("python, -senior", "Senior Python dev", False),
        ("-crypto", "Telegram bot for a cafe", True),
        ("c++", "C++ developer", True),
        ("node.js", "Fix my Node.js API", True),
    ],
)
def test_matches(keywords: str, title: str, expected: bool) -> None:
    assert matches(parse_keywords(keywords), item(title)) is expected


def test_matches_searches_summary_and_fields() -> None:
    rule = parse_keywords("remote")
    assert matches(rule, item("Bot", summary="Fully remote role"))
    assert matches(rule, item("Bot", location="Remote"))
    assert not matches(rule, item("Bot", location="Berlin"))
