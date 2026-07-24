import pytest
from router.main import _count_tokens_heuristic

@pytest.mark.parametrize("text, expected", [
    (None, 0.0),
    ("", 0.0),
    (123, 0.0),
    ([], 0.0),
    ({}, 0.0),
    ("hello world", 2.4),
    ("word", 1.2),
    ("hellooooooooo", 13 / 4.0),
    ("superlongword", 13 / 4.0),
    ("😊", 0.35),
    ("你好", 0.70),
    ("🚀", 0.35),
    (",.!", 1.2),
    (".,;", 1.2),
    ("Hello, world! 😊", 1.2 + 0.4 + 1.2 + 0.4 + 0.35),
    ("mix!🚀", 1.2 + 0.4 + 0.35),
])
def test_count_tokens_heuristic(text, expected):
    assert _count_tokens_heuristic(text) == pytest.approx(expected)
