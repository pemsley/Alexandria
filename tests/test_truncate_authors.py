"""_truncate_authors fills the available line with whole names to a
character budget instead of a fixed count of four, ending with
", et al." only when names were actually dropped."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria.author_works import _truncate_authors


def test_empty():
    assert _truncate_authors([]) == ""


def test_all_fit_no_et_al():
    names = ["A. One", "B. Two", "C. Three"]
    assert _truncate_authors(names) == "A. One, B. Two, C. Three"


def test_short_names_fit_more_than_four():
    names = ["Nm {}".format(i) for i in range(10)]   # ~6 chars each
    out = _truncate_authors(names)
    assert out == ", ".join(names)                   # all 10 fit


def test_overflow_truncates_with_et_al():
    names = ["Author Nameson {:02d}".format(i) for i in range(40)]
    out = _truncate_authors(names)
    assert out.endswith(", et al.")
    shown = out[:-len(", et al.")].split(", ")
    assert 1 <= len(shown) < 40
    # More generous than the old fixed four.
    assert len(shown) > 4


def test_budget_is_respected():
    names = ["Author Nameson {:02d}".format(i) for i in range(40)]
    assert len(_truncate_authors(names, max_chars=80)) <= 80


def test_at_least_one_name_even_when_over_budget():
    names = ["A" * 200, "B. Short"]
    out = _truncate_authors(names, max_chars=50)
    assert out.startswith("A" * 200)
    assert out.endswith(", et al.")


def test_smaller_budget_shows_fewer():
    names = ["Author Nameson {:02d}".format(i) for i in range(40)]
    wide = _truncate_authors(names, max_chars=110)
    narrow = _truncate_authors(names, max_chars=60)
    assert len(narrow) < len(wide)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
