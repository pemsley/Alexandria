"""Tests for the `filter=` string metrics.search_works builds — the
author constraint on the "By title" Discover tab, and the comma
escaping that keeps a filter value from truncating the query.

Runnable as `python3 -m tests.test_search_works_filters` (no pytest
required) or collectable by pytest.
"""

import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


def _capture(**kwargs):
    """Call search_works with the network stubbed out; return the
    parsed query parameters of the URL it would have fetched."""
    seen = {}

    def fake_get(url, headers=None, timeout=None, raise_on_quota=False):
        seen["url"] = url
        return {"results": []}

    real = metrics._http_get_json
    metrics._http_get_json = fake_get
    try:
        metrics.search_works(**kwargs)
    finally:
        metrics._http_get_json = real
    if "url" not in seen:
        return None
    qs = urllib.parse.urlparse(seen["url"]).query
    return dict(urllib.parse.parse_qsl(qs))


def _filters(params):
    return (params or {}).get("filter", "").split(",")


# ---- Author constraint ----

def test_author_adds_raw_author_name_filter():
    p = _capture(query="hierarchical clustering", search_field="title",
                 author="Emsley")
    assert "raw_author_name.search:Emsley" in _filters(p), p


def test_author_composes_with_title_and_year():
    p = _capture(query="Coot model building", search_field="title",
                 author="Emsley", year_min=2010)
    f = _filters(p)
    assert "title.search:Coot model building" in f, f
    assert "raw_author_name.search:Emsley" in f, f
    assert "from_publication_date:2010-01-01" in f, f


def test_author_composes_with_free_text_search():
    p = _capture(query="antibiotic resistance", author="Smith")
    assert p["search"] == "antibiotic resistance", p
    assert "raw_author_name.search:Smith" in _filters(p), p


def test_no_author_means_no_author_filter():
    p = _capture(query="Coot model building", search_field="title")
    assert not any(x.startswith("raw_author_name") for x in _filters(p)), p


def test_blank_author_is_ignored():
    p = _capture(query="Coot model building", search_field="title",
                 author="   ")
    assert not any(x.startswith("raw_author_name") for x in _filters(p)), p


# ---- Comma escaping ----
#
# OpenAlex reads `,` as the AND separator between filters, so an
# unescaped comma in a value silently splits it into two filters and
# returns the wrong works.

def test_comma_in_author_does_not_split_the_filter():
    p = _capture(query="Coot model building", search_field="title",
                 author="Emsley, P.")
    f = _filters(p)
    assert "raw_author_name.search:Emsley P." in f, f
    # No stray filter made of the fragment after the comma.
    assert all(":" in x for x in f), f


def test_comma_in_title_does_not_split_the_filter():
    p = _capture(query="Features and development of Coot, revisited",
                 search_field="title")
    f = _filters(p)
    assert "title.search:Features and development of Coot revisited" in f, f
    assert all(":" in x for x in f), f


def test_pipe_is_neutralised_too():
    # `|` is OpenAlex's OR separator within one filter value.
    p = _capture(query="a|b", search_field="title", author="x|y")
    f = _filters(p)
    assert "title.search:a b" in f, f
    assert "raw_author_name.search:x y" in f, f


def test_title_of_only_separators_makes_no_request():
    assert _capture(query=" , ", search_field="title") is None


def test_free_text_query_keeps_its_comma():
    # The top-level `search=` parameter is not a filter — commas are
    # just urlencoded there, so nothing should be stripped.
    p = _capture(query="Coot, revisited")
    assert p["search"] == "Coot, revisited", p


# ---- Self-test runner ----

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
