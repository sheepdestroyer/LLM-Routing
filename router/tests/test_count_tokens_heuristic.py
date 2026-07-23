import pytest
from main import _count_tokens_heuristic

def test_count_tokens_heuristic_empty():
    assert _count_tokens_heuristic(None) == 0.0
    assert _count_tokens_heuristic("") == 0.0

def test_count_tokens_heuristic_short_words():
    # "hello" and "world" are both length 5 (<= 8) -> 1.2 each
    assert _count_tokens_heuristic("hello world") == pytest.approx(2.4)

def test_count_tokens_heuristic_long_words():
    # "hellooooooooo" is length 13 (> 8) -> 13 / 4.0 = 3.25
    assert _count_tokens_heuristic("hellooooooooo") == pytest.approx(3.25)

def test_count_tokens_heuristic_non_ascii():
    # "😊" is 1 non-ascii character -> 0.35
    # "你好" is 2 non-ascii characters -> 0.70
    assert _count_tokens_heuristic("😊") == pytest.approx(0.35)
    assert _count_tokens_heuristic("你好") == pytest.approx(0.70)

def test_count_tokens_heuristic_punctuation():
    # ",.!" are 3 punctuation characters -> 3 * 0.4 = 1.2
    assert _count_tokens_heuristic(",.!") == pytest.approx(1.2)

def test_count_tokens_heuristic_mixed():
    # "Hello, world! 😊"
    # "Hello" (1.2) + "," (0.4) + "world" (1.2) + "!" (0.4) + "😊" (0.35)
    # Total = 1.2 + 0.4 + 1.2 + 0.4 + 0.35 = 3.55
    assert _count_tokens_heuristic("Hello, world! 😊") == pytest.approx(3.55)

@pytest.mark.parametrize("text, expected", [
    ("word", 1.2),
    ("superlongword", 13 / 4.0),
    (".,;", 1.2),
    ("🚀", 0.35),
    ("mix!🚀", 1.2 + 0.4 + 0.35),
])
def test_count_tokens_heuristic_parametrized(text, expected):
    assert _count_tokens_heuristic(text) == pytest.approx(expected)
