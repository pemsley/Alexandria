"""Volume, issue and pages — stored, editable, exported.

Reported 2026-09-05 from a real export: `~/alexandria-export.bib`
carries 178 entries and **not one** volume, number or pages field,
though 146 of them have a DOI. They were never captured, never
stored and so never exported.

Three sources were already within reach and all three unused:

  * CrossRef's `volume` / `issue` / `page`, from the record we
    already fetch for licence and funder information.
  * OpenAlex's `biblio` block, from the record we already fetch for
    citation counts.
  * The PRISM XMP block inside the PDF, which `extract` already
    lifts into `rec["raw"]` — 9 of 60 sampled sidecars have
    `volume` and `pageRange` sitting there unread.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import bibtex_export, csl, metrics, ris_export, sidecar


# ---- the record carries them -----------------------------------------

def test_a_new_record_has_the_three_fields(tmp_path):
    rec = sidecar.new_record(str(tmp_path / "p.pdf"))
    for f in ("volume", "issue", "pages"):
        assert f in rec, f
        assert rec[f] is None


# ---- reading them off the sources we already fetch --------------------

def test_crossref_volume_issue_and_page():
    msg = {"DOI": "10.1/x", "title": ["T"], "container-title": ["J"],
           "volume": "94", "issue": "4", "page": "713-730"}
    w = metrics._work_from_crossref_message(msg)
    assert w["volume"] == "94"
    assert w["issue"] == "4"
    assert w["pages"] == "713-730"


def test_crossref_without_them_is_not_an_error():
    w = metrics._work_from_crossref_message(
        {"DOI": "10.1/x", "title": ["T"]})
    assert w["volume"] is None and w["issue"] is None
    assert w["pages"] is None


def test_openalex_biblio_is_read():
    """OpenAlex splits the page range into two fields."""
    b = metrics.biblio_from_openalex(
        {"biblio": {"volume": "65", "issue": None,
                    "first_page": "209", "last_page": "216"}})
    assert b["volume"] == "65"
    assert b["pages"] == "209-216"
    assert b["issue"] is None


def test_openalex_single_page_article():
    b = metrics.biblio_from_openalex(
        {"biblio": {"volume": "619", "first_page": "112814",
                    "last_page": "112814"}})
    assert b["pages"] == "112814", "not '112814-112814'"


def test_openalex_with_no_biblio_block():
    assert metrics.biblio_from_openalex({}) == {
        "volume": None, "issue": None, "pages": None}


# ---- salvaging what is already in the sidecar ------------------------

PRISM = "http://prismstandard.org/namespaces/basic/3.0/"


def test_prism_xmp_already_in_raw_is_used():
    """Elsevier PDFs carry this; `extract` already stores it and
    nothing has ever read it."""
    raw = {PRISM: {"volume": "94", "number": "4",
                   "pageRange": "713-730", "startingPage": "713",
                   "endingPage": "730"}}
    b = metrics.biblio_from_raw(raw)
    assert b["volume"] == "94"
    assert b["issue"] == "4"
    assert b["pages"] == "713-730"


def test_prism_start_and_end_when_there_is_no_range():
    raw = {PRISM: {"startingPage": "209", "endingPage": "216"}}
    assert metrics.biblio_from_raw(raw)["pages"] == "209-216"


def test_raw_without_prism():
    assert metrics.biblio_from_raw({"Creator": "x"}) == {
        "volume": None, "issue": None, "pages": None}
    assert metrics.biblio_from_raw(None)["volume"] is None


# ---- the exporters use them ------------------------------------------

REC = {"title": "T", "authors": ["Jane Smith"], "year": 2017,
       "journal": "Neuron", "doi": "10.1/x",
       "volume": "94", "issue": "4", "pages": "713--730"}


def test_bibtex_export_emits_them():
    r = bibtex_export.sidecar_to_bibtex_record(dict(REC))
    extra = r["bibtex_extra"]
    assert extra["volume"] == "94"
    assert extra["number"] == "4", "BibTeX calls the issue 'number'"
    assert extra["pages"] == "713--730"


def test_a_record_field_beats_a_stale_bibtex_extra():
    """A BibTeX-imported paper has both. The edited field wins —
    otherwise correcting a page range in the dialog would appear to
    do nothing on export."""
    rec = dict(REC, bibtex_extra={"volume": "OLD", "pages": "1--2"})
    r = bibtex_export.sidecar_to_bibtex_record(rec)
    assert r["bibtex_extra"]["volume"] == "94"
    assert r["bibtex_extra"]["pages"] == "713--730"


def test_bibtex_extra_still_supplies_a_record_with_no_fields():
    """The round-trip work of 2026-09-02 must keep working for
    papers imported from BibTeX before these fields existed."""
    rec = {"title": "T", "authors": ["A B"], "year": 2020,
           "bibtex_extra": {"volume": "5", "pages": "1--9"}}
    r = bibtex_export.sidecar_to_bibtex_record(rec)
    assert r["bibtex_extra"]["volume"] == "5"
    assert r["bibtex_extra"]["pages"] == "1--9"


def test_ris_export_uses_them():
    lines = dict(ris_export.sidecar_to_ris_lines(dict(REC)))
    assert lines.get("VL") == "94"
    assert lines.get("IS") == "4"


def test_csl_export_uses_them():
    out = csl.sidecar_to_csl(dict(REC))
    assert out.get("volume") == "94"
    assert out.get("issue") == "4"
    assert out.get("page") == "713-730"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
