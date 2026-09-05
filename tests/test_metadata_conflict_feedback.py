"""A detected metadata conflict must reach the user, not just the log.

Reported 2026-09-05. At startup the citation refresher prints

    [citations] OpenAlex record for 10.1016/S0957-5820(99)70836-0
    looks corrupted — skipping refresh

and then does nothing else: no chip on the card, nothing in the
Edit-metadata dialog. The user sees a paper whose metadata is wrong
with no indication anywhere that the app already knows.

The machinery exists. `browse.make_metadata_chip` renders an amber
"Check metadata" chip from `metadata_conflict`, with a popover
comparing both versions — but only `importer` ever writes that
field, at import time. The refresher runs the same comparison and
just logs.

The worked example is instructive because the filename held the
answer: a file whose own PII said `S0959440X99000202` carried
`doi: 10.1016/S0957-5820(99)70836-0`, which resolves to a Book
Review in a different journal entirely.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import importer


# ---- recording what the refresher found -------------------------------

def test_a_mismatch_becomes_a_conflict_record():
    rec = {"title": "Advances in direct methods for protein "
                    "crystallography",
           "year": 1999,
           "doi": "10.1016/S0957-5820(99)70836-0",
           "authors": ["George M. Sheldrick"]}
    out = importer.conflict_from_refresh(
        rec, oa_title="Book Review", oa_year=1999)
    assert out is not None
    assert out["doi_title"] == "Book Review"
    assert out["stored_title"] == rec["title"]
    assert out["doi"] == rec["doi"]


def test_the_source_is_recorded_so_the_two_can_be_told_apart():
    """The importer writes this field too, comparing the PDF against
    the DOI. A refresh compares the *stored* record against what the
    DOI says now — a different question, and the popover should not
    claim the PDF said something it never said."""
    out = importer.conflict_from_refresh(
        {"title": "Advances in direct methods for protein "
                  "crystallography", "year": 1999, "doi": "10.1/x"},
        oa_title="Book Review", oa_year=1999)
    assert out["found_by"] == "refresh"


def test_agreement_is_not_a_conflict():
    rec = {"title": "Same Title", "year": 2001, "doi": "10.1/x"}
    assert importer.conflict_from_refresh(
        rec, oa_title="Same Title", oa_year=2001) is None


def test_openalex_knowing_nothing_is_not_a_conflict():
    """An empty OpenAlex record is a gap, not a disagreement — the
    refresher already declines to act on one."""
    rec = {"title": "A Title", "year": 2001, "doi": "10.1/x"}
    assert importer.conflict_from_refresh(
        rec, oa_title=None, oa_year=None) is None


def test_a_hand_edited_record_is_left_alone():
    """The user's own answer settles it; make_metadata_chip already
    suppresses the chip for these, so writing the field would be
    noise that never surfaces."""
    rec = {"title": "Advances in direct methods for protein "
                    "crystallography", "year": 1999, "doi": "10.1/x",
           "hand_edited": True}
    assert importer.conflict_from_refresh(
        rec, oa_title="Book Review", oa_year=1999) is None


# ---- the filename often holds the answer ------------------------------

def test_a_filename_doi_that_disagrees_is_carried_as_evidence():
    """The Elsevier PII in the filename gave the right paper where
    the stored DOI gave a Book Review. When the two disagree, say
    so — it turns "something is wrong" into "here is the answer"."""
    rec = {"title": "Advances in direct methods", "year": 1999,
           "doi": "10.1016/S0957-5820(99)70836-0"}
    out = importer.conflict_from_refresh(
        rec, oa_title="Book Review", oa_year=1999,
        pdf_path="/lib/advances-1-s2.0-S0959440X99000202-main.pdf")
    assert out["filename_doi"] == "10.1016/s0959-440x(99)00020-2"


def test_no_filename_doi_is_simply_absent():
    out = importer.conflict_from_refresh(
        {"title": "Advances in direct methods for protein "
                  "crystallography", "year": 1999, "doi": "10.1/x"},
        oa_title="Book Review", oa_year=1999,
        pdf_path="/lib/scan001.pdf")
    assert out.get("filename_doi") is None


def test_a_filename_doi_matching_the_stored_one_is_not_evidence():
    rec = {"title": "Advances in direct methods for protein "
                    "crystallography", "year": 1999,
           "doi": "10.1016/s0959-440x(99)00020-2"}
    out = importer.conflict_from_refresh(
        rec, oa_title="Book Review", oa_year=1999,
        pdf_path="/lib/1-s2.0-S0959440X99000202-main.pdf")
    assert out.get("filename_doi") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
