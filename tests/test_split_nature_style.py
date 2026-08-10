"""Tests for viewer._split_entry_text's Nature-style branch and the
organisation-author detector used to skip find_doi's author gate.

Nature style puts the year at the very end ("Authors. Title. Journal
Abbrev. Vol, pages (Year).") so the generic parser recovered an empty
title. These lock in title extraction (via the lowercase-word selector)
and the personal-vs-organisation distinction.

Runnable as `python3 -m tests.test_split_nature_style` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria.viewer import _split_entry_text, _looks_personal


def test_org_author_title_extracted():
    # The wwPDB entry that used to MISS (W2898210859).
    a, y, t, j = _split_entry_text(
        "wwPDB Consortium. Protein Data Bank: the single global archive "
        "for 3D macromolecular structure data. Nucleic Acids Res. 47, "
        "D520-D528 (2018).")
    assert t == ("Protein Data Bank: the single global archive for 3D "
                 "macromolecular structure data")
    assert y == 2018
    assert a == "wwPDB Consortium"
    assert j == "Nucleic Acids Res"


def test_personal_author_title_extracted():
    a, y, t, j = _split_entry_text(
        "Bai, X.-C., McMullan, G. & Scheres, S. H. W. How cryo-EM is "
        "revolutionizing structural biology. Trends Biochem. Sci. 40, "
        "49-57 (2015).")
    assert t == "How cryo-EM is revolutionizing structural biology"
    assert y == 2015
    assert j == "Trends Biochem. Sci"


def test_single_author_title_extracted():
    a, y, t, j = _split_entry_text(
        "Sippl, M. J. Calculation of conformational ensembles from "
        "potentials of mean force. J. Mol. Biol. 213, 859-883 (1990).")
    assert t == ("Calculation of conformational ensembles from potentials "
                 "of mean force")
    assert y == 1990


def test_page_prefixed_pages_are_handled():
    # Pages like "D520-D528" (letter-prefixed) must still be recognised as
    # the volume/pages tail.
    _, _, t, _ = _split_entry_text(
        "Author, A. Some distinctive title here about things. "
        "Nucleic Acids Res. 47, D1-D9 (2020).")
    assert t == "Some distinctive title here about things"


def test_non_nature_style_falls_through():
    # A Cell-style entry (year after authors) must NOT be caught by the
    # Nature branch — it has no year at the end.
    a, y, t, j = _split_entry_text(
        "Aldridge, S., and Teichmann, S. A. (2020). Single cell "
        "transcriptomics comes of age. Nat. Commun. 11, 4307.")
    assert y == 2020
    assert "Single cell transcriptomics" in (t or "")


def test_missing_year_returns_none_tuple():
    assert _split_entry_text("No year anywhere in this text.") == (
        None, None, None, None)


def test_looks_personal_true_for_surname_initials():
    assert _looks_personal("Bai, X.-C., McMullan, G. & Scheres, S. H. W.")
    assert _looks_personal("Sippl, M. J.")


def test_looks_personal_false_for_organisation():
    assert not _looks_personal("wwPDB Consortium")
    assert not _looks_personal("RCSB Protein Data Bank")
    assert not _looks_personal("")


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
