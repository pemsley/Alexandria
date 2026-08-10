"""Tests for extract._scrape_doi, in particular DOIs that wrap across a
line break at a hyphen ("…111622-\\n091155"). A DOI never ends in a
hyphen, so a hyphen-terminated fragment at a line break is a wrap and
must be stitched to the continuation on the next line.

Runnable as `python3 -m tests.test_scrape_doi` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import extract


def test_hyphen_line_wrap_is_stitched():
    text = "https://doi.org/10.1146/annurev-biophys-111622-\n091155\n"
    assert extract._scrape_doi(text) == "10.1146/annurev-biophys-111622-091155"


def test_hyphen_wrap_with_layout_alignment_whitespace():
    # `pdftotext -layout` right-aligns the DOI and the continuation line
    # carries leading alignment spaces plus a neighbouring column.
    text = ("            https://doi.org/10.1146/annurev-biophys-111622-\n"
            "            091155                         Abstract\n")
    assert extract._scrape_doi(text) == "10.1146/annurev-biophys-111622-091155"


def test_single_line_doi_unchanged():
    assert extract._scrape_doi(
        "see https://doi.org/10.1038/nature12373 here") == "10.1038/nature12373"


def test_non_hyphen_line_end_not_over_stitched():
    # A complete DOI that merely ends a line must NOT absorb the next line.
    assert extract._scrape_doi(
        "10.1038/nature12373\nSomething else entirely") == "10.1038/nature12373"


def test_doi_prefix_forms_stripped():
    assert extract._scrape_doi("doi:10.1000/xyz123") == "10.1000/xyz123"


def test_publisher_doi_preferred_over_data_doi():
    text = ("Data at 10.5281/zenodo.123456 . "
            "Article https://doi.org/10.1038/s41586-021-03819-2 .")
    assert extract._scrape_doi(text) == "10.1038/s41586-021-03819-2"


def test_no_doi_returns_none():
    assert extract._scrape_doi("no identifier here") is None
    assert extract._scrape_doi("") is None


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
