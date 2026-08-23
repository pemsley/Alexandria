"""Tests for the numeric in-text citation finder — the path that makes
"(6, 7)" clickable in papers whose PDF carries no internal Link
annotations at all.

AAAS/Science is the motivating case: those PDFs are produced by iText
with every single annotation a `/URI` link out to the web, so there is
nothing for `pdf_links.read_citation_links` to find and the reader is
left with dead citations. The finder recovers them from the page text
instead.

Precision matters more than recall here — a citation that jumps to the
wrong reference is worse than one that doesn't jump at all — so most
of what follows checks that plausible-looking text is *rejected*.

Runnable as `python3 -m tests.test_numeric_citations` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import references_pdf as R


def _parts(body):
    """(entry_n, start, end) triples -> just the numbers, for brevity."""
    return [n for n, _s, _e in R._numeric_parts(body)]


# ---- Splitting a citation body into per-number spans ----

def test_single_number():
    assert _parts("42") == [42]


def test_comma_list_yields_one_entry_each():
    # The whole point of per-part spans: clicking "7" in "(6, 7)"
    # should go to reference 7, not reference 6.
    assert _parts("6, 7") == [6, 7]


def test_range_contributes_its_first_entry():
    # There's no way to tell which of 8..14 the reader meant, so the
    # opening one is the useful guess.
    assert _parts("8-14") == [8]
    assert _parts("8–14") == [8]      # en dash
    assert _parts("8—14") == [8]      # em dash


def test_mixed_list_of_ranges_and_singles():
    assert _parts("1-3, 5, 15, 16") == [1, 5, 15, 16]


def test_spans_cover_the_number_not_the_whole_token():
    body = "6, 7"
    parts = R._numeric_parts(body)
    assert [body[s:e] for _n, s, e in parts] == ["6", "7"]


def test_spans_skip_leading_whitespace():
    body = "1-3, 5"
    parts = R._numeric_parts(body)
    assert [body[s:e] for _n, s, e in parts] == ["1-3", "5"]


def test_empty_and_junk_parts_are_dropped():
    assert _parts("") == []
    assert _parts(",,") == []
    assert _parts("-") == []


# ---- Which bracketed tokens are citations at all ----

def _tok(text):
    m = R._NUMERIC_CITE_RE.search(text)
    return m.group(1) if m else None


def test_parenthetical_and_square_forms_both_match():
    assert _tok("humans (6, 7). And") == "6, 7"
    assert _tok("humans [6, 7]. And") == "6, 7"


def test_non_numeric_bodies_do_not_match():
    # These are the everyday parenthetical asides that must not become
    # citation links.
    for t in ["we examine (i) the length", "(Fig. 1A) shows",
              "(fig. S1)", "(ILS)", "(see below)"]:
        assert _tok(t) is None, t


def test_cross_reference_prefixes_are_rejected():
    for t in ["as in Eq. (1)", "Equation (12)", "Fig. (3)", "Table (2)"]:
        m = R._NUMERIC_CITE_RE.search(t)
        assert m is not None, t          # the token itself matches …
        assert R._NOT_A_CITATION_BEFORE_RE.search(t[:m.start()]), t   # … but is vetoed


def test_a_citation_after_a_closing_paren_is_not_vetoed():
    # "(Fig. 1A) (42)" — the veto looks at the text before the token,
    # which here ends in ")", not "Fig.".
    t = "the admixture event (Fig. 1A) (42). Second"
    m = R._NUMERIC_CITE_RE.search(t, t.index("(42)"))
    assert not R._NOT_A_CITATION_BEFORE_RE.search(t[:m.start()])


# ---- The bibliography-plausibility guard ----

def _entries(texts):
    return [{"n": i + 1, "text": t, "page": 5, "y_top_poppler": 100.0}
            for i, t in enumerate(texts)]


def test_a_real_reference_list_passes():
    bib = _entries([
        "M. Meyer, M. Kircher, Science 338, 222 (2012).",
        "K. Pruefer et al., Nature 505, 43 (2014).",
        "R. E. Green et al., Science 328, 710 (2010).",
        "A. Author, J. Mol. Biol. 123, 45 (1984).",
    ])
    assert R._looks_like_reference_list(bib) is True


def test_a_manuals_numbered_list_is_rejected():
    # The Amber22 failure: parse_bibliography picked up the numbered
    # items of an introduction and called them a bibliography, which
    # would have made every "(1)" in the manual a link to item 1.
    bib = _entries([
        "Introduction Amber is the collective name for a suite of programs",
        "Topology: Connectivity, atom names, atom types, residue names",
        "Force field: Parameters for all of the bonds, angles, dihedrals",
        "Once the topology and coordinate files have been prepared",
    ])
    assert R._looks_like_reference_list(bib) is False


def test_an_empty_bibliography_is_rejected():
    assert R._looks_like_reference_list([]) is False


def test_the_threshold_is_a_majority_not_unanimity():
    # Real lists do contain the odd year-less entry (a URL, a dataset).
    bib = _entries([
        "A. Author, Journal 1, 2 (2001).",
        "B. Author, Journal 3, 4 (2002).",
        "C. Author, Journal 5, 6 (2003).",
        "Zenodo dataset, no year given here.",
    ])
    assert R._looks_like_reference_list(bib) is True


# ---- expand_citation_token (the pre-existing helper) ----

def test_expand_citation_token_handles_ranges_and_brackets():
    assert R.expand_citation_token("[9-12]") == [9, 10, 11, 12]
    assert R.expand_citation_token("1, 3, 5-7") == [1, 3, 5, 6, 7]


# ---- Self-test runner ----

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print("FAIL  {}\n        {}".format(t.__name__, e))
        except Exception as e:
            failures += 1
            print("ERROR {}\n        {!r}".format(t.__name__, e))
        else:
            print("ok    {}".format(t.__name__))
    print()
    print("{} test(s), {} failure(s)".format(len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
