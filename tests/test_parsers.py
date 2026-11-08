import pytest
from conftest import fixture_bytes

from tgwatch.errors import ParseError
from tgwatch.models import ParseSpec, WatchKind
from tgwatch.parsers import parse

BASE = "https://jobs.example.com/feed.xml"


def test_rss_items_links_summary_and_extra_fields() -> None:
    items = parse(
        WatchKind.RSS, fixture_bytes("feed.rss"), BASE, ParseSpec(fields={"by": "author"})
    )
    assert [i.title for i in items] == [
        "Python developer for a Telegram bot",
        "Senior Java engineer",
        "JavaScript widget fix",
    ]
    first = items[0]
    assert first.uid == "job-101"
    assert first.summary == "Build an aiogram bot. Remote, part time."
    assert first.fields == {"by": "board@example.com (Sample Board)"}
    # No guid: the link is the identity, resolved against the feed URL.
    assert items[2].uid == items[2].link == "https://jobs.example.com/jobs/103"


def test_atom_feed() -> None:
    items = parse(WatchKind.RSS, fixture_bytes("feed.atom"), BASE, ParseSpec())
    assert [(i.title, i.link) for i in items] == [
        ("v2.0.0", "https://example.com/releases/2.0.0"),
        ("v1.9.0", "https://example.com/releases/1.9.0"),
    ]
    assert items[0].summary == "Breaking: drops Python 3.10."


def test_html_page_is_not_a_feed() -> None:
    with pytest.raises(ParseError, match="RSS/Atom"):
        parse(WatchKind.RSS, b"<html><body>hello</body></html>", BASE, ParseSpec())


def test_html_cards_with_field_selectors() -> None:
    spec = ParseSpec(items="li.card", fields={"title": ".card__title", "price": ".card__price"})
    items = parse(
        WatchKind.HTML, fixture_bytes("listings.html"), "https://ads.example.com/s?q=bike", spec
    )
    assert [(i.title, i.link, i.fields) for i in items] == [
        ('Used bicycle, 26"', "https://ads.example.com/ad/1", {"price": "120 USD"}),
        ("Road bike, carbon", "https://ads.example.com/ad/2", {"price": "900 USD"}),
        ("Kids bike", "https://other.example.org/ad/3", {}),
        ("Sponsored", "", {}),
    ]


def test_html_defaults_to_item_text_and_first_link() -> None:
    items = parse(
        WatchKind.HTML,
        fixture_bytes("listings.html"),
        "https://ads.example.com/",
        ParseSpec(items="a.card__link"),
    )
    assert items[0].title == 'Used bicycle, 26"'
    assert items[0].link == "https://ads.example.com/ad/1"


def test_html_requires_selector_and_rejects_bad_one() -> None:
    with pytest.raises(ParseError, match="needs a CSS selector"):
        parse(WatchKind.HTML, b"<p>x</p>", BASE, ParseSpec())
    with pytest.raises(ParseError, match="invalid CSS selector"):
        parse(WatchKind.HTML, b"<p>x</p>", BASE, ParseSpec(items="li[["))


def test_html_duplicates_are_collapsed() -> None:
    page = b'<a class="x" href="/1">One</a><a class="x" href="/1">One again</a>'
    items = parse(WatchKind.HTML, page, "https://e.example/", ParseSpec(items="a.x"))
    assert len(items) == 1


def test_json_items_with_default_and_custom_fields() -> None:
    spec = ParseSpec(items="data.children[*].data", fields={"budget": "budget", "remote": "remote"})
    items = parse(
        WatchKind.JSON, fixture_bytes("api.json"), "https://www.example.com/r/sample.json", spec
    )
    assert [(i.uid, i.title, i.link, i.fields) for i in items] == [
        (
            "a1",
            "Looking for a Flutter dev",
            "https://www.example.com/r/sample/a1",
            {"budget": "300", "remote": "yes"},
        ),
        (
            "a2",
            "Need a logo",
            "https://www.example.com/r/sample/a2",
            {"budget": "50", "remote": "no"},
        ),
        # Non-scalar values are dropped instead of printed as Python reprs.
        ("a3", "Fix my Node.js API", "https://example.com/a3", {}),
    ]


def test_json_path_to_list_without_wildcard() -> None:
    body = b'{"results": [{"id": 7, "title": "x"}]}'
    items = parse(WatchKind.JSON, body, BASE, ParseSpec(items="results"))
    assert [i.uid for i in items] == ["7"]


def test_json_top_level_list_and_bad_json() -> None:
    assert len(parse(WatchKind.JSON, b'[{"id": 1}, {"id": 2}, 3]', BASE, ParseSpec())) == 2
    with pytest.raises(ParseError, match="not valid JSON"):
        parse(WatchKind.JSON, b"<html>", BASE, ParseSpec())
