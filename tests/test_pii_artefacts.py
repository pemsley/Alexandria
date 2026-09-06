"""Elsevier's back-catalogue re-exports assert two things falsely.

A 1995 paper re-exported in the mid-2000s arrives with
`CreationDate D:20050105…` and a title of `PII:
S0022-2836(95)80037-9`. Both are confident and both are wrong.
Confidently wrong is worse than absent here: downstream, a missing
year gets filled from OpenAlex, while "2005" is believed and shown.

Measured on the real file
`1-s2.0-S0022283695800379-molecular-recognition-of-receptor-site.pdf`
(Jones, Willett & Glen, JMB 1995), whose sidecar read `year: 2005`.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from alexandria import extract

PII = "S0022-2836(95)80037-9"


# --- the year encoded in the identifier -----------------------------------

@pytest.mark.parametrize("text,expected", [
    (PII, 1995),
    ("PII: " + PII, 1995),
    ("S0022283695800379", 1995),
    ("10.1016/s0022-2836(95)80037-9", 1995),
    ("1-s2.0-S0022283695800379-molecular-recognition.pdf", 1995),
    ("S1047-8477(05)00071-2", 2005),
    ("S0006-3495(66)86653-4", 1966),
])
def test_the_pii_states_the_year(text, expected):
    assert extract.pii_year(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "no identifier here", "10.1038/nature12373",
])
def test_no_pii_no_year(text):
    assert extract.pii_year(text) is None


# --- a PII is not a title -------------------------------------------------

@pytest.mark.parametrize("title", [
    "PII: " + PII, PII, "  pii:  " + PII + "  ", "S0022283695800379",
])
def test_a_pii_is_recognised_as_not_a_title(title):
    assert extract.looks_like_pii_title(title) is True


@pytest.mark.parametrize("title", [
    None, "",
    "Molecular recognition of receptor sites",
    # a real title that merely mentions one
    "Erratum to PII: " + PII,
])
def test_a_real_title_is_left_alone(title):
    assert extract.looks_like_pii_title(title) is False


# --- the rule applied -----------------------------------------------------

def test_the_reported_record_loses_both_artefacts():
    rec = extract.drop_pii_artefacts(
        {"title": "PII: " + PII, "year": 2005,
         "doi": "10.1016/s0022-2836(95)80037-9"},
        "/lib/1-s2.0-S0022283695800379-molecular-recognition.pdf")
    assert rec["title"] is None
    assert rec["year"] is None      # unknown, so OpenAlex can fill it


def test_it_is_a_ceiling_not_a_correction():
    """A year at or before the PII's is the file telling the truth."""
    rec = extract.drop_pii_artefacts(
        {"title": "A real title", "year": 1995, "doi": None},
        "/lib/1-s2.0-S0022283695800379-paper.pdf")
    assert rec["year"] == 1995
    rec = extract.drop_pii_artefacts(
        {"title": "A real title", "year": 1994, "doi": None},
        "/lib/1-s2.0-S0022283695800379-paper.pdf")
    assert rec["year"] == 1994


def test_papers_with_no_pii_are_untouched():
    rec = extract.drop_pii_artefacts(
        {"title": "Features and development of Coot", "year": 2010,
         "doi": "10.1107/S0907444910007493"}, "/lib/coot.pdf")
    assert rec["title"] == "Features and development of Coot"
    assert rec["year"] == 2010


# --- the DOI was in the filename all along --------------------------------

ELSEVIER_NAME = "1-s2.0-S0022283695800379-molecular-recognition.pdf"


def test_the_elsevier_filename_decodes_to_the_doi():
    """The PII in the filename *is* the DOI, in a different dress."""
    assert extract.doi_from_filename("/lib/" + ELSEVIER_NAME) == \
        "10.1016/s0022-2836(95)80037-9"


def test_extraction_used_the_decoder_that_could_not_do_this():
    """Why `_enrich` chains both: the private decoder extraction has
    always used does not know this form, which is how a paper whose
    DOI sat in its own filename imported with `doi: null`."""
    assert extract._doi_from_filename("/lib/" + ELSEVIER_NAME) is None
    chained = (extract._doi_from_filename("/lib/" + ELSEVIER_NAME)
               or extract.doi_from_filename("/lib/" + ELSEVIER_NAME))
    assert chained == "10.1016/s0022-2836(95)80037-9"


def test_the_private_decoder_still_wins_where_it_is_the_better_one():
    """Neither is a superset, so order matters and is not arbitrary."""
    assert extract._doi_from_filename("/lib/pnas-0502225102.pdf") == \
        "10.1073/pnas.0502225102"
