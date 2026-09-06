"""Refresh must not overwrite a stored field with nothing.

Reported 2026-08-27 against Jones, Willett & Glen (JMB 1995): the
user pasted the DOI into the metadata dialog and pressed refresh,
expecting OpenAlex to fill in the rest. The sidecar came back with
`doi: null`.

`refresh_pdf` rebuilds the record from a fresh extraction of the PDF
and then restores a fixed list of user-curated keys. `doi`, `title`,
`authors`, `year` and `journal` are deliberately not on that list,
so that a better extraction can improve them — but the PDF in
question yields no DOI at all, so "improve" meant "erase".

A blank replacing a value is never an improvement. That is the rule
tested here; it is narrower than adding these keys to the preserve
list, which would stop extraction improving them at all.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from alexandria import importer, index, sidecar


@pytest.fixture
def paper(tmp_path, monkeypatch):
    """A sidecar carrying hand-entered metadata, and a PDF extraction
    that finds nothing — the reported situation."""
    pdf = str(tmp_path / "molecular-recognition.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    sc = sidecar.sidecar_path_for(pdf)
    rec = sidecar.new_record(pdf)
    rec.update({
        "title": "Molecular recognition of receptor sites",
        "authors": ["Gareth Jones", "Peter Willett", "Robert C. Glen"],
        "year": 1995,
        "journal": "Journal of Molecular Biology",
        "doi": "10.1006/jmbi.1995.0037",
        "volume": "245",
        "issue": "1",
        "pages": "43--53",
    })
    sidecar.write(sc, rec)
    conn = index.open_db(str(tmp_path / "lib.db"))
    index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))

    for name, value in (
            ("fetch_metrics", lambda d: (None,) * 12),
            ("is_preprint_doi", lambda d: False)):
        monkeypatch.setattr(importer.metrics, name, value)
    monkeypatch.setattr(importer.thumbnail, "make_thumbnail",
                        lambda *a, **k: None)
    monkeypatch.setattr(importer, "_schedule_pdb_indexing", lambda *a: None)
    monkeypatch.setattr(
        importer.jats, "fetch_and_store",
        lambda p, d: {"status": "no_pmcid", "pmcid": None,
                      "checked": "2026-09-06"})
    return conn, pdf, sc


def test_extraction_finding_nothing_keeps_every_typed_field(paper, monkeypatch):
    conn, pdf, sc = paper
    monkeypatch.setattr(importer, "_build_record", lambda p: {})

    rec, status = importer.refresh_pdf(conn, pdf)

    assert status == "refreshed"
    stored = sidecar.read(sc)
    assert stored["doi"] == "10.1006/jmbi.1995.0037"
    assert stored["title"] == "Molecular recognition of receptor sites"
    assert stored["year"] == 1995
    assert stored["journal"] == "Journal of Molecular Biology"
    assert stored["authors"][0] == "Gareth Jones"
    assert (stored["volume"], stored["issue"], stored["pages"]) == \
        ("245", "1", "43--53")


@pytest.mark.parametrize("blank", [None, "", [], 0])
def test_every_shape_of_empty_counts_as_nothing(paper, monkeypatch, blank):
    """Extractors signal "not found" in several ways."""
    conn, pdf, sc = paper
    monkeypatch.setattr(importer, "_build_record",
                        lambda p: {"doi": blank, "year": blank})

    importer.refresh_pdf(conn, pdf)

    stored = sidecar.read(sc)
    assert stored["doi"] == "10.1006/jmbi.1995.0037"
    assert stored["year"] == 1995


def test_a_real_extraction_still_wins(paper, monkeypatch):
    """The point of refresh is that re-reading the PDF can improve
    the record; only *blank* results are ignored."""
    conn, pdf, sc = paper
    monkeypatch.setattr(
        importer, "_build_record",
        lambda p: {"title": "A better title from the PDF",
                   "doi": "10.1006/jmbi.1995.9999"})

    importer.refresh_pdf(conn, pdf)

    stored = sidecar.read(sc)
    assert stored["title"] == "A better title from the PDF"
    assert stored["doi"] == "10.1006/jmbi.1995.9999"


def test_preserved_doi_drives_the_openalex_enrichment(paper, monkeypatch):
    """The other half of the report: "there is no path from I know
    the DOI to fetch the metadata". With the DOI no longer erased,
    the enrichment pass that follows it in refresh_pdf now runs on
    the typed value."""
    conn, pdf, sc = paper
    monkeypatch.setattr(importer, "_build_record", lambda p: {})
    seen = []
    monkeypatch.setattr(
        importer.metrics, "fetch_metrics",
        lambda doi: seen.append(doi) or (
            41, "openalex", ["docking"], "An abstract.", None, None,
            "Molecular recognition of receptor sites", 1995,
            True, "green", None, None))

    importer.refresh_pdf(conn, pdf)

    assert seen == ["10.1006/jmbi.1995.0037"]
    stored = sidecar.read(sc)
    assert stored["citations"] == 41
    assert stored["abstract"] == "An abstract."
