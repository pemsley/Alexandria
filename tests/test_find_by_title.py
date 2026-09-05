"""Searching OpenAlex from the metadata already in the dialog.

Reported 2026-09-03: pasting an author list into "Find metadata"
returned Hierarchical Linear Models and The Economics of Climate
Change for a crystallography paper, because
`parse_citation_hint` read the surname as "Nicholas".

The fix asked for is not a cleverer parser. The Edit dialog already
holds Title, Year and Journal, correctly separated — it needs no
parsing at all. And the title is nearly decisive on its own: the
paper that failed has a title that matches exactly one work in
OpenAlex.

`find_citation_candidates` takes only surname/year/journal, so this
adds the title route beside it.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


# ---- building the query ---------------------------------------------

def _filter_of(url):
    """Just the filter= value. The select= list legitimately contains
    commas and the word publication_year, so assertions have to look
    at the filter alone."""
    import urllib.parse
    q = urllib.parse.urlparse(url).query
    return urllib.parse.parse_qs(q)["filter"][0]


def test_the_title_is_the_filter():
    url = metrics.title_search_url("Features and development of Coot")
    assert "title.search:" in url
    assert "Features" in url


def test_a_year_narrows_it():
    url = metrics.title_search_url(
        "Features and development of Coot", year=2010)
    assert "publication_year:2010" in url


def test_no_year_leaves_it_open():
    url = metrics.title_search_url("Features and development of Coot")
    assert "publication_year" not in _filter_of(url)


def test_punctuation_that_would_break_the_filter_is_dropped():
    """OpenAlex filter values are comma- and pipe-separated, so a
    title containing either would be read as two filters."""
    filt = _filter_of(metrics.title_search_url("Coot: model-building, tools"))
    assert "," not in filt and "|" not in filt
    assert "model-building" in filt


def test_a_title_too_short_to_identify_anything_is_refused():
    """One or two words match half of OpenAlex; that is the failure
    mode this whole change exists to avoid. A paper with a title
    this short still has the typed-fragment box."""
    assert metrics.title_search_url("") is None
    assert metrics.title_search_url("   ") is None
    assert metrics.title_search_url(None) is None
    assert metrics.title_search_url("Coot") is None


def test_a_real_title_is_long_enough():
    assert metrics.title_search_url(
        "Features and development of Coot") is not None


# ---- describing what will be searched, for the hint line ------------

def test_the_hint_names_the_fields_it_will_use():
    d = metrics.describe_metadata_search(
        {"title": "Features and development of Coot", "year": 2010,
         "journal": "Acta Cryst D"})
    assert "title" in d.lower()
    assert "2010" in d


def test_the_hint_says_when_there_is_only_a_title():
    d = metrics.describe_metadata_search({"title": "A Long Enough Title"})
    assert "title" in d.lower()
    assert "2010" not in d


def test_no_usable_metadata_is_reported_as_such():
    assert metrics.describe_metadata_search({}) is None
    assert metrics.describe_metadata_search({"title": "Coot"}) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
