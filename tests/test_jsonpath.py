import pytest

from tgwatch.errors import ParseError
from tgwatch.jsonpath import compile_path

DOC = {
    "data": {"children": [{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": []}]},
    "key with spaces": {"x": 5},
    "map": {"one": {"n": 1}, "two": {"n": 2}},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", [DOC]),
        ("$", [DOC]),
        ("data.children[0].id", [1]),
        ("$.data.children[-1].id", [2]),
        ("$.data.children[*].id", [1, 2]),
        ("data.children[*].tags[*]", ["a", "b"]),
        ('$["key with spaces"].x', [5]),
        ("map.*.n", [1, 2]),
        ("data.missing.id", []),
        ("data.children[9]", []),
        ("data.children.id", []),
    ],
)
def test_find(path: str, expected: list[object]) -> None:
    assert compile_path(path).find(DOC) == expected


def test_first_and_wildcard_flag() -> None:
    path = compile_path("data.children[*].id")
    assert path.has_wildcard
    assert path.first(DOC) == 1
    assert compile_path("nope").first(DOC) is None


@pytest.mark.parametrize("bad", ["data..x", "data[", "data[abc]", "a b"])
def test_invalid_paths_raise(bad: str) -> None:
    with pytest.raises(ParseError):
        compile_path(bad)
