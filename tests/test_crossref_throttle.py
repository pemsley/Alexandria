"""Tests for the proactive CrossRef rate-limit throttle in
pdforg.metrics.

Runnable as `python3 -m tests.test_crossref_throttle` (no pytest
required) or collectable by pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pdforg import metrics


# ---- _parse_crossref_interval -------------------------------------

def test_interval_bare_number():
    assert metrics._parse_crossref_interval("5") == 5.0


def test_interval_seconds():
    assert metrics._parse_crossref_interval("1s") == 1.0
    assert metrics._parse_crossref_interval("10s") == 10.0


def test_interval_minutes_hours():
    assert metrics._parse_crossref_interval("1m") == 60.0
    assert metrics._parse_crossref_interval("2h") == 7200.0


def test_interval_bad_falls_back_to_default():
    d = metrics._CROSSREF_DEFAULT_INTERVAL
    assert metrics._parse_crossref_interval(None) == d
    assert metrics._parse_crossref_interval("") == d
    assert metrics._parse_crossref_interval("abc") == d
    assert metrics._parse_crossref_interval("0s") == d   # non-positive


# ---- _note_crossref_rate ------------------------------------------

def test_note_rate_updates_both():
    saved = (metrics._crossref_limit, metrics._crossref_interval)
    try:
        metrics._note_crossref_rate(
            {"X-Rate-Limit-Limit": "30", "X-Rate-Limit-Interval": "2s"})
        assert metrics._crossref_limit == 30
        assert metrics._crossref_interval == 2.0
    finally:
        metrics._crossref_limit, metrics._crossref_interval = saved


def test_note_rate_missing_limit_leaves_it():
    saved = (metrics._crossref_limit, metrics._crossref_interval)
    try:
        metrics._crossref_limit = 50
        metrics._note_crossref_rate({"X-Rate-Limit-Interval": "4s"})
        assert metrics._crossref_limit == 50      # unchanged
        assert metrics._crossref_interval == 4.0  # updated
    finally:
        metrics._crossref_limit, metrics._crossref_interval = saved


def test_note_rate_no_headers_is_noop():
    saved = (metrics._crossref_limit, metrics._crossref_interval)
    try:
        metrics._crossref_limit = 50
        metrics._crossref_interval = 1.0
        metrics._note_crossref_rate({})
        metrics._note_crossref_rate(None)
        assert metrics._crossref_limit == 50
        assert metrics._crossref_interval == 1.0
    finally:
        metrics._crossref_limit, metrics._crossref_interval = saved


# ---- _crossref_wait_seconds ---------------------------------------

def test_wait_empty_window():
    assert metrics._crossref_wait_seconds([], now=100.0,
                                          limit=50, interval=1.0) == 0.0


def test_wait_under_limit():
    times = [99.9, 99.95, 99.99]
    assert metrics._crossref_wait_seconds(
        times, now=100.0, limit=50, interval=1.0) == 0.0


def test_wait_at_limit_returns_until_oldest_expires():
    # limit 2, two requests inside the 1s window; oldest at 99.6 so it
    # ages out at 100.6 → wait 0.6s.
    times = [99.6, 99.8]
    w = metrics._crossref_wait_seconds(times, now=100.0,
                                       limit=2, interval=1.0)
    assert abs(w - 0.6) < 1e-9


def test_wait_prunes_stale_timestamps():
    # Two stale (older than now-interval) + one live; under limit=2
    # after pruning the stale ones, so no wait.
    times = [98.0, 98.5, 99.9]
    w = metrics._crossref_wait_seconds(times, now=100.0,
                                       limit=2, interval=1.0)
    assert w == 0.0


# ---- Self-test runner ---------------------------------------------

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
