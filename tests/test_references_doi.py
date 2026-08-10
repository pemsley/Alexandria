"""Tests for references_pdf._normalize_doi — in particular that a
citation year glued onto a DOI by whitespace-collapse ("…26171(2021)")
is stripped, while parens that are genuinely part of the DOI body
("…S0022-2836(05)80269-4") are preserved.

Runnable as `python3 -m tests.test_references_doi` (no pytest required)
or collectable by pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import references_pdf as R


def _via_regex(text):
    """Match a DOI out of `text` the way parse_bibliography does, then
    normalise it — exercises the whole extract+clean path."""
    m = R._DOI_RE.search(text)
    return R._normalize_doi(m.group(0)) if m else None


def test_trailing_year_paren_is_stripped_closed():
    # "…prot.26171 (2021)." with the space collapsed into the DOI body.
    assert _via_regex("10.1002/prot.26171(2021).") == "10.1002/prot.26171"


def test_trailing_year_paren_is_stripped_open():
    # The already-corrupted shape (earlier ')' trimming left "(2021").
    assert R._normalize_doi("10.1002/prot.26171(2021") == "10.1002/prot.26171"


def test_nature_style_trailing_year():
    assert _via_regex("10.1038/s41586-021-03828-1(2021).") == \
        "10.1038/s41586-021-03828-1"


def test_biorxiv_style_trailing_year():
    assert _via_regex("10.1101/2021.05.10.443524(2021).") == \
        "10.1101/2021.05.10.443524"


def test_mid_body_parens_are_preserved():
    # The classic case the fix must NOT break.
    assert _via_regex("10.1016/S0022-2836(05)80269-4") == \
        "10.1016/s0022-2836(05)80269-4"


def test_mid_body_parens_preserved_with_trailing_year():
    # Both at once: keep the body parens, drop the trailing year.
    assert R._normalize_doi("10.1016/S0022-2836(05)80269-4(2005)") == \
        "10.1016/s0022-2836(05)80269-4"


def test_plain_doi_unchanged():
    assert _via_regex("10.1038/nprot.2010.5") == "10.1038/nprot.2010.5"


def test_trailing_sentence_punctuation_still_trimmed():
    assert _via_regex("see 10.1093/nar/gkz1035.") == "10.1093/nar/gkz1035"


def test_none_and_empty():
    assert R._normalize_doi(None) is None
    assert R._normalize_doi("") is None


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
