"""Tests for metrics.find_doi_by_citation — the Crossref
`query.bibliographic` fallback that resolves raw numbered/Vancouver/
Nature-style references to a DOI.

Runnable as `python3 -m tests.test_find_doi_by_citation` (no pytest
required) or collectable by pytest. No network: the HTTP layer is
monkeypatched to return canned Crossref `/works` messages.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


# A Nature-style entry: authors "Surname, I. I." groups, title, journal,
# year in parens at the very end. The kind _split_entry_text can't parse.
NATURE_ENTRY = (
    "Bai, X.-C., McMullan, G. & Scheres, S. H. W. How cryo-EM is "
    "revolutionizing structural biology. Trends Biochem. Sci. 40, "
    "49–57 (2015).")

# The matching Crossref top hit (trimmed): first-author family "Bai".
MSG_MATCH = {"message": {"items": [{
    "DOI": "10.1016/j.tibs.2014.10.005",
    "score": 95.5,
    "title": ["How cryo-EM is revolutionizing structural biology"],
    "author": [
        {"given": "Xiao-chen", "family": "Bai", "sequence": "first"},
        {"given": "Greg", "family": "McMullan", "sequence": "additional"},
    ],
}]}}

# A confident-but-wrong Crossref hit: the classic DBSCAN citation
# ("Ester, M., et al. …") matched to a different clustering paper whose
# first author ("Ram") is nowhere in the entry text.
DBSCAN_ENTRY = (
    "Ester, M., et al. A density-based algorithm for discovering "
    "clusters in large spatial databases with noise. In, Kdd. 1996. "
    "p. 226-231.")
MSG_WRONG = {"message": {"items": [{
    "DOI": "10.5120/739-1038",
    "score": 50.9,
    "title": ["A Density Based Algorithm for Discovering Density "
              "Varied Clusters"],
    "author": [{"given": "A.", "family": "Ram", "sequence": "first"}],
}]}}


def _with_response(response):
    """Swap metrics._http_get_json for one returning `response`, and
    record the URL it was called with. Returns (restore_fn, calls)."""
    calls = []
    saved = metrics._http_get_json

    def fake(url, headers, timeout, raise_on_quota=False):
        calls.append(url)
        return response

    metrics._http_get_json = fake
    return (lambda: setattr(metrics, "_http_get_json", saved), calls)


def test_accepts_when_first_author_surname_in_text():
    restore, _ = _with_response(MSG_MATCH)
    try:
        doi = metrics.find_doi_by_citation(NATURE_ENTRY)
    finally:
        restore()
    assert doi == "10.1016/j.tibs.2014.10.005"


def test_rejects_when_first_author_surname_absent():
    # The gate must drop the wrong match even though Crossref returned a
    # DOI — "Ram" is not in the DBSCAN entry text.
    restore, _ = _with_response(MSG_WRONG)
    try:
        doi = metrics.find_doi_by_citation(DBSCAN_ENTRY)
    finally:
        restore()
    assert doi is None


def test_surname_gate_is_whole_word():
    # A short surname that only appears as a substring must not match.
    # "Ng" is a substring of "designing"/"engineering" but not a word.
    entry = ("Designing and engineering robust systems for large "
             "spatial workloads. J. Test. 1, 1 (2020).")
    msg = {"message": {"items": [{
        "DOI": "10.1/x",
        "author": [{"given": "K.", "family": "Ng"}],
    }]}}
    restore, _ = _with_response(msg)
    try:
        doi = metrics.find_doi_by_citation(entry)
    finally:
        restore()
    assert doi is None


def test_returns_none_on_no_items():
    restore, _ = _with_response({"message": {"items": []}})
    try:
        doi = metrics.find_doi_by_citation(NATURE_ENTRY)
    finally:
        restore()
    assert doi is None


def test_returns_none_on_http_failure():
    restore, _ = _with_response(None)
    try:
        doi = metrics.find_doi_by_citation(NATURE_ENTRY)
    finally:
        restore()
    assert doi is None


def test_short_text_skips_network():
    restore, calls = _with_response(MSG_MATCH)
    try:
        doi = metrics.find_doi_by_citation("Bai 2015")
    finally:
        restore()
    assert doi is None
    assert calls == []          # never hit the network for trivial input


def test_missing_author_family_rejected():
    msg = {"message": {"items": [{
        "DOI": "10.1/x",
        "author": [{"given": "Only Given, No Family"}],
    }]}}
    restore, _ = _with_response(msg)
    try:
        doi = metrics.find_doi_by_citation(NATURE_ENTRY)
    finally:
        restore()
    assert doi is None


def test_doi_url_is_normalised():
    msg = {"message": {"items": [{
        "DOI": "https://doi.org/10.1016/j.tibs.2014.10.005",
        "author": [{"family": "Bai"}],
    }]}}
    restore, _ = _with_response(msg)
    try:
        doi = metrics.find_doi_by_citation(NATURE_ENTRY)
    finally:
        restore()
    assert doi == "10.1016/j.tibs.2014.10.005"


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
