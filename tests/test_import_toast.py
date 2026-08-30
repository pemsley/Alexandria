"""Tests for alexandria.import_toast coalescing logic.

Runnable as `python3 -m tests.test_import_toast` (no pytest required) or
collectable by pytest. Each test is a top-level `test_*` function.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import import_toast


def test_single_start_shows_named_toast():
    assert import_toast.toast_action(["a.pdf"]) == ("name", "a.pdf")


def test_second_start_shows_named_toast_for_newest():
    assert import_toast.toast_action(["a.pdf", "b.pdf"]) == ("name", "b.pdf")


def test_third_start_collapses_to_count():
    assert import_toast.toast_action(
        ["a.pdf", "b.pdf", "c.pdf"]) == ("count", 3)


def test_further_starts_bump_count():
    names = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"]
    assert import_toast.toast_action(names) == ("count", 5)


def test_empty_window_is_noop():
    assert import_toast.toast_action([]) == ("noop", None)


# ---- Self-test runner (no pytest needed) ---------------------------



# ---- duplicate starts for one file ---------------------------------

def test_record_start_adds_a_new_name():
    names, is_new = import_toast.record_start(["a.pdf"], "b.pdf")
    assert names == ["a.pdf", "b.pdf"]
    assert is_new is True


def test_record_start_ignores_a_repeat_of_the_same_file():
    """One dropped PDF reaches the watcher twice — CREATED and
    CHANGES_DONE_HINT — and the drop handler imports it as well.
    Each start used to queue its own toast, so the user saw
    'Importing x…' appear, vanish, and appear again."""
    names, is_new = import_toast.record_start(["a.pdf"], "a.pdf")
    assert names == ["a.pdf"]
    assert is_new is False


def test_repeats_do_not_inflate_the_collapsed_count():
    names = []
    for n in ("a.pdf", "a.pdf", "b.pdf", "b.pdf", "a.pdf"):
        names, _ = import_toast.record_start(names, n)
    assert names == ["a.pdf", "b.pdf"]
    assert import_toast.toast_action(names) == ("name", "b.pdf")


def test_three_distinct_files_still_collapse():
    names = []
    for n in ("a.pdf", "b.pdf", "b.pdf", "c.pdf"):
        names, _ = import_toast.record_start(names, n)
    assert import_toast.toast_action(names) == ("count", 3)

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        name = t.__name__
        try:
            t()
        except AssertionError as e:
            failures += 1
            print("FAIL  {}\n        {}".format(name, e))
        except Exception as e:
            failures += 1
            print("ERROR {}\n        {!r}".format(name, e))
        else:
            print("ok    {}".format(name))
    print()
    print("{} test(s), {} failure(s)".format(len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
