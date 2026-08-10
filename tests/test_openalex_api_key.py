"""Tests for OpenAlex API-key injection in alexandria.metrics.

Covers the pure URL-rewriting helper `_apply_openalex_key` and the
env/runtime precedence of `set_openalex_api_key`. No network.

Runnable as `python3 -m tests.test_openalex_api_key` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


def _set_key(key):
    """Force the module key regardless of env precedence (tests drive
    the value directly)."""
    metrics._OPENALEX_API_KEY = key


def test_key_appended_to_bare_openalex_url():
    saved = metrics._OPENALEX_API_KEY
    try:
        _set_key("abc123")
        out = metrics._apply_openalex_key("https://api.openalex.org/works/W1")
        assert out == "https://api.openalex.org/works/W1?api_key=abc123"
    finally:
        metrics._OPENALEX_API_KEY = saved


def test_key_appended_with_ampersand_when_query_present():
    saved = metrics._OPENALEX_API_KEY
    try:
        _set_key("abc123")
        out = metrics._apply_openalex_key(
            "https://api.openalex.org/works?filter=x&mailto=a@b.com")
        assert out == (
            "https://api.openalex.org/works?filter=x&mailto=a@b.com"
            "&api_key=abc123")
    finally:
        metrics._OPENALEX_API_KEY = saved


def test_key_is_url_quoted():
    saved = metrics._OPENALEX_API_KEY
    try:
        _set_key("a b/c")
        out = metrics._apply_openalex_key("https://api.openalex.org/works/W1")
        assert out.endswith("?api_key=a%20b/c")
    finally:
        metrics._OPENALEX_API_KEY = saved


def test_no_key_leaves_url_unchanged():
    saved = metrics._OPENALEX_API_KEY
    try:
        _set_key("")
        url = "https://api.openalex.org/works/W1"
        assert metrics._apply_openalex_key(url) == url
    finally:
        metrics._OPENALEX_API_KEY = saved


def test_non_openalex_url_never_gets_key():
    saved = metrics._OPENALEX_API_KEY
    try:
        _set_key("abc123")
        url = "https://api.crossref.org/works/10.1/x"
        assert metrics._apply_openalex_key(url) == url
    finally:
        metrics._OPENALEX_API_KEY = saved


def test_existing_api_key_not_doubled():
    saved = metrics._OPENALEX_API_KEY
    try:
        _set_key("new")
        url = "https://api.openalex.org/works/W1?api_key=old"
        assert metrics._apply_openalex_key(url) == url
    finally:
        metrics._OPENALEX_API_KEY = saved


def test_set_openalex_api_key_sets_value():
    saved = metrics._OPENALEX_API_KEY
    saved_env = os.environ.pop("ALEXANDRIA_OPENALEX_API_KEY", None)
    try:
        metrics.set_openalex_api_key("  fromprefs  ")
        assert metrics.openalex_api_key() == "fromprefs"
    finally:
        metrics._OPENALEX_API_KEY = saved
        if saved_env is not None:
            os.environ["ALEXANDRIA_OPENALEX_API_KEY"] = saved_env


def test_env_var_wins_over_runtime_set():
    saved = metrics._OPENALEX_API_KEY
    os.environ["ALEXANDRIA_OPENALEX_API_KEY"] = "fromenv"
    try:
        metrics._OPENALEX_API_KEY = "fromenv"     # as import would have set it
        metrics.set_openalex_api_key("fromprefs")  # must be ignored
        assert metrics.openalex_api_key() == "fromenv"
    finally:
        metrics._OPENALEX_API_KEY = saved
        os.environ.pop("ALEXANDRIA_OPENALEX_API_KEY", None)


# ---- Self-test runner (no pytest needed) ---------------------------


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
