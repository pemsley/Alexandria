""""I know the DOI — go and look it up."

Reported 2026-08-27: the user pasted a DOI into the metadata dialog
and pressed Refresh, which means "re-read the PDF" and so threw the
DOI away. The affordance they reached for did not exist. This covers
the Fetch button beside the DOI entry: what counts as a DOI when a
human types one, that the OpenAlex reply carries the fields the
dialog fills, and that the button actually fills them.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from alexandria import metrics

DOI = "10.1006/jmbi.1995.0037"


# --- what a person pastes -------------------------------------------------

@pytest.mark.parametrize("typed", [
    DOI,
    "  " + DOI + "  ",
    "doi:" + DOI,
    "DOI: " + DOI,
    "https://doi.org/" + DOI,
    "http://dx.doi.org/" + DOI,
    "https://www.doi.org/" + DOI,
    DOI + ".",                       # copied from the end of a sentence
    "(" + DOI + ")",
    "see " + DOI + ", which shows",
])
def test_every_way_a_doi_arrives(typed):
    assert metrics.normalise_typed_doi(typed) == DOI


def test_a_dot_inside_the_suffix_survives():
    """`.` is legal in a suffix, so only a trailing one is stripped."""
    assert metrics.normalise_typed_doi("10.1107/S2059798324008659") == \
        "10.1107/S2059798324008659"
    assert metrics.normalise_typed_doi("10.1002/prot.24941.") == \
        "10.1002/prot.24941"


@pytest.mark.parametrize("junk", [
    "", None, "   ", "not a doi", "10.1006", "jmbi.1995.0037",
    "https://example.org/paper.pdf",
])
def test_nothing_that_is_not_a_doi_gets_through(junk):
    """Reported to the user rather than sent to OpenAlex."""
    assert metrics.normalise_typed_doi(junk) is None


# --- the reply the dialog fills from --------------------------------------

_WORK = {
    "id": "https://openalex.org/W2119207841",
    "doi": "https://doi.org/" + DOI,
    "title": "Molecular recognition of receptor sites",
    "publication_year": 1995,
    "publication_date": "1995-01-13",
    "primary_location": {"source": {"display_name":
                                    "Journal of Molecular Biology"}},
    "authorships": [
        {"author": {"display_name": "Gareth Jones"},
         "author_position": "first"},
        {"author": {"display_name": "Peter Willett"}},
        {"author": {"display_name": "Robert C. Glen"},
         "author_position": "last"},
    ],
    "biblio": {"volume": "245", "issue": "1",
               "first_page": "43", "last_page": "53"},
    "cited_by_count": 1470,
    "open_access": {"is_oa": False},
}


def test_the_reply_carries_every_field_the_dialog_fills(monkeypatch):
    monkeypatch.setattr(metrics, "_http_get_json",
                        lambda url, **kw: _WORK)
    w = metrics.fetch_work_by_doi(DOI)
    assert w["doi"] == DOI          # normalised out of the URL form
    assert w["title"] == "Molecular recognition of receptor sites"
    assert w["year"] == 1995
    assert w["journal"] == "Journal of Molecular Biology"
    assert w["authors"] == ["Gareth Jones", "Peter Willett",
                            "Robert C. Glen"]
    assert (w["volume"], w["issue"], w["pages"]) == ("245", "1", "43-53")


def test_unknown_doi_is_a_None_not_an_exception(monkeypatch):
    monkeypatch.setattr(metrics, "_http_get_json", lambda url, **kw: None)
    assert metrics.fetch_work_by_doi(DOI) is None
