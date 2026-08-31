import pytest
import asyncio
from router.main import _load_aa_scores, load_aa_scores_async

def test_load_aa_scores():
    _load_aa_scores()

if __name__ == "__main__":
    test_load_aa_scores()
