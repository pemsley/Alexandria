"""Typing in the search bar should not query on every keystroke.

Reported 2026-09-01: the second and third characters take a
noticeable while to appear. `_on_search` was wired straight to
`search-changed` and called `_reload`, so every keystroke ran a full
`index.search` and rebuilt the card list — and the cost was worst
exactly where it was least useful, because a one-character query
matches almost the whole library and returns the full 500-row limit.
So the first keystroke fired the most expensive query of the
sequence and the next two queued behind it.

Two rules, and both are needed: a minimum length stops the useless
expensive queries, and a debounce stops the rest of the word firing
one query per letter.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import reload_policy as rp


# ---- what a typed string means ---------------------------------------

def test_a_short_query_shows_everything_rather_than_filtering():
    """Below the threshold the list is unfiltered — not frozen on the
    last result, which would leave a stale view behind a query the
    user has deleted back to nothing."""
    for s in ("", "a", "ab", "   ", " a "):
        assert rp.search_query(s) is None, repr(s)


def test_three_characters_is_enough():
    assert rp.search_query("abc") == "abc"
    assert rp.search_query("coot") == "coot"


def test_surrounding_whitespace_does_not_count():
    assert rp.search_query("  abc  ") == "abc"
    assert rp.search_query("  ab  ") is None


def test_none_is_no_query():
    assert rp.search_query(None) is None


def test_the_threshold_can_be_lowered_for_a_deliberate_search():
    """A filter chip sets the box programmatically and must apply
    whatever it says — a two-letter surname is still a real
    filter."""
    assert rp.search_query("Wu", min_chars=1) == "Wu"


# ---- how long to wait before running it ------------------------------

def test_typing_is_debounced():
    assert rp.SEARCH_DEBOUNCE_MS > 0


def test_the_search_debounce_is_shorter_than_the_watcher_one():
    """Typing is burstier than a filesystem event, and the reader is
    waiting on this one — 300 ms between letters reads as lag."""
    assert rp.SEARCH_DEBOUNCE_MS < rp.DEBOUNCE_MS


def test_it_is_not_so_short_that_it_fires_mid_word():
    """Fast typing runs at roughly 100 ms per character."""
    assert rp.SEARCH_DEBOUNCE_MS >= 100


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
