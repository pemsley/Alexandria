"""Choosing between DOI candidates for a PDF.

A PDF can offer two DOIs that disagree: the one in its text or Info
dict, and the one encoded in its download filename. Measured across
the library, neither wins in general — filename-first would have
fixed 2 papers and broken 11 (Elsevier PII vs canonical j-style,
bioRxiv's 10.1101 -> 10.64898 prefix change). So resolve the
candidates and keep the one whose record matches what the PDF says
about itself.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import importer

USON = "Advances in direct methods for protein crystallography"
REFEREES = "List of Referees 1999"


def _rec(title, authors=("A. Author",), year=1999):
    return {"title": title, "authors": list(authors), "year": year}


def _resolver(table):
    return lambda doi: table.get(doi)


# ---- single candidate ----------------------------------------------

def test_single_candidate_that_resolves_is_accepted():
    got = importer.choose_doi_candidate(
        USON, 1999, ["10.1/right.001"], _resolver({"10.1/right.001": _rec(USON)}))
    assert got.doi == "10.1/right.001"
    assert got.record["title"] == USON
    assert got.agrees is True


def test_single_candidate_is_still_accepted_when_the_pdf_text_is_junk():
    """The Chem Pharm Bull case: correct DOI, extracted title is the
    running header. Accept the record — but say it didn't agree."""
    got = importer.choose_doi_candidate(
        "Chem. Pharm. Bull. 74(6): 513-528 (2026)", 2026,
        ["10.1248/cpb.c26-00122"],
        _resolver({"10.1248/cpb.c26-00122": _rec("Design, Synthesis",
                                                 year=2026)}))
    assert got.doi == "10.1248/cpb.c26-00122"
    assert got.record is not None
    assert got.agrees is False


def test_candidate_with_no_record_leaves_us_unverified():
    got = importer.choose_doi_candidate(
        USON, 1999, ["10.1/nothing.001"], _resolver({}))
    assert got.doi == "10.1/nothing.001"
    assert got.record is None
    assert got.agrees is False


# ---- two candidates -------------------------------------------------

def test_matching_candidate_beats_the_first_one():
    """The Uson case: the Info dict's DOI resolves to a Book Review;
    the filename's PII resolves to the actual paper."""
    got = importer.choose_doi_candidate(
        USON, 1999,
        ["10.1016/s0957-5820(99)70836-0",
         "10.1016/s0959-440x(99)00020-2"],
        _resolver({
            "10.1016/s0957-5820(99)70836-0": _rec("Book Review"),
            "10.1016/s0959-440x(99)00020-2": _rec(USON),
        }))
    assert got.doi == "10.1016/s0959-440x(99)00020-2"
    assert got.agrees is True


def test_first_candidate_wins_when_it_is_the_matching_one():
    """The Neuron case: the canonical j-style DOI from the text
    matches; the filename's PII form must not displace it."""
    got = importer.choose_doi_candidate(
        "Cortical circuits", 2017,
        ["10.1016/j.neuron.2017.04.009",
         "10.1016/s0896-6273(17)30302-1"],
        _resolver({
            "10.1016/j.neuron.2017.04.009": _rec("Cortical circuits",
                                                 year=2017),
            "10.1016/s0896-6273(17)30302-1": None,
        }))
    assert got.doi == "10.1016/j.neuron.2017.04.009"


def test_no_candidate_matches_keeps_the_first_and_flags_it():
    got = importer.choose_doi_candidate(
        USON, 1999, ["10.1/a001", "10.1/b002"],
        _resolver({"10.1/a001": _rec("Something else"),
                   "10.1/b002": _rec("Also unrelated")}))
    assert got.doi == "10.1/a001"
    assert got.agrees is False


def test_a_resolving_candidate_beats_a_non_resolving_first_one():
    got = importer.choose_doi_candidate(
        "Whatever", 2020, ["10.1/dead.001", "10.1/live.002"],
        _resolver({"10.1/live.002": _rec("Whatever", year=2020)}))
    assert got.doi == "10.1/live.002"


# ---- records that aren't papers -------------------------------------

def test_journal_level_record_is_not_usable():
    """'10.1073/pnas' — a truncated DOI that resolves to the journal
    itself. It returns a record, so 'did it resolve' cannot catch it;
    the absence of authors can."""
    got = importer.choose_doi_candidate(
        "PNAS202122660_proof.pdf", 2022, ["10.1073/pnas"],
        _resolver({"10.1073/pnas": _rec(
            "Proceedings of the National Academy of Sciences",
            authors=())}))
    assert got.record is None
    assert got.agrees is False


# ---- degenerate input ------------------------------------------------

def test_no_candidates_at_all():
    got = importer.choose_doi_candidate(USON, 1999, [], _resolver({}))
    assert got.doi is None
    assert got.record is None
    assert got.agrees is False


def test_duplicate_candidates_are_resolved_once():
    calls = []

    def resolve(doi):
        calls.append(doi)
        return _rec(USON)

    got = importer.choose_doi_candidate(
        USON, 1999, ["10.1/x001", "10.1/X001", "10.1/x001"], resolve)
    assert got.doi == "10.1/x001"
    assert len(calls) == 1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ---- the PDF's text is a better witness than its Info dict ---------

def test_pdf_text_breaks_the_tie_when_the_extracted_title_lies():
    """The Uson case in full: the Info dict supplies BOTH a wrong
    title ("List of Referees 1999") and a wrong DOI, so comparing
    against the extracted title can't discriminate — but the paper's
    real title is right there in the page text."""
    text = ("Advances in direct methods for protein crystallography\n"
            "Isabel Uson and George M Sheldrick\n"
            "Recent advances in ab initio direct methods…")
    got = importer.choose_doi_candidate(
        REFEREES, 1999,
        ["10.1016/s0957-5820(99)70836-0",
         "10.1016/s0959-440x(99)00020-2"],
        _resolver({
            "10.1016/s0957-5820(99)70836-0": _rec("Book Review"),
            "10.1016/s0959-440x(99)00020-2": _rec(USON),
        }),
        pdf_text=text)
    assert got.doi == "10.1016/s0959-440x(99)00020-2"
    assert got.agrees is True


def test_pdf_text_does_not_rescue_an_unrelated_record():
    got = importer.choose_doi_candidate(
        REFEREES, 1999, ["10.1/a001"],
        _resolver({"10.1/a001": _rec("Something quite different")}),
        pdf_text="Advances in direct methods for protein crystallography")
    assert got.agrees is False


def test_truncated_doi_naming_only_the_journal_is_refused():
    """'10.1073/pnas' resolves to a record that even has authors (an
    editorial roster), so the author test alone let it through and
    labelled a fragment-screening paper 'Proceedings of the National
    Academy of Sciences'. The tell is in the DOI: a real article
    suffix carries digits, a bare journal token doesn't."""
    resolved = []

    def resolve(doi):
        resolved.append(doi)
        return {"title": "Proceedings of the National Academy of Sciences",
                "authors": ["A. Aiuti"], "year": 2022}

    got = importer.choose_doi_candidate(
        "PNAS202122660_proof.pdf", 2022, ["10.1073/pnas"], resolve)
    assert got.record is None
    assert resolved == [], "should not even be looked up"
