"""The ordinary import path: one candidate DOI, nothing to choose.

`_enrich_from_openalex` resolves between two disagreeing DOIs — the
one in the PDF and the one in its filename — and binds `choice` while
doing so. When there is only one candidate, no choice is made, and a
later read of `choice` raised

    cannot access local variable 'choice' where it is not associated
    with a value

which failed the import outright. Reported 2026-09-06 against four
files at once; the volume/issue/pages work (a7b16d2) introduced it,
and only the contested-DOI path was covered by tests.

Volume/issue/pages must still arrive on this path, which is the one
almost every import takes.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from alexandria import importer

DOI = "10.1038/s41467-021-24715-3"

# fetch_metrics' 12-tuple, with a usable record (authorships present).
METRICS = (7, "openalex", ["kw"], "An abstract.",
           [{"name": "A. Author", "openalex_id": "A1"}], None,
           "A paper title", 2021, True, "gold", None, None)


@pytest.fixture
def one_candidate(monkeypatch):
    monkeypatch.setattr(importer.extract, "doi_from_filename",
                        lambda p: None)
    monkeypatch.setattr(importer.metrics, "fetch_metrics",
                        lambda d: METRICS)
    monkeypatch.setattr(importer.metrics, "oa_author_names",
                        lambda a, existing=None: ["A. Author"])
    return {"doi": DOI, "title": "A paper title", "year": 2021,
            "authors": ["A. Author"]}


def test_a_single_candidate_doi_imports(one_candidate, monkeypatch):
    monkeypatch.setattr(importer.metrics, "resolve_doi", lambda d: None)
    rec = one_candidate

    importer._enrich_from_openalex(rec, "/lib/s41467-021-24715-3.pdf")

    assert rec["doi"] == DOI
    assert rec["citations"] == 7
    assert "metadata_unverified" not in rec


def test_biblio_still_arrives_without_a_contested_doi(one_candidate,
                                                      monkeypatch):
    """The rare contested import must not be the only one that gets
    volume/issue/pages."""
    monkeypatch.setattr(
        importer.metrics, "resolve_doi",
        lambda d: {"volume": "12", "issue": "1", "pages": "4471"})
    rec = one_candidate

    importer._enrich_from_openalex(rec, "/lib/s41467-021-24715-3.pdf")

    assert (rec["volume"], rec["issue"], rec["pages"]) == ("12", "1", "4471")


def test_pages_from_the_pdf_save_the_round_trip(one_candidate, monkeypatch):
    """Publishers that stamp PRISM into the file already answered."""
    asked = []
    monkeypatch.setattr(importer.metrics, "resolve_doi",
                        lambda d: asked.append(d) or {})
    rec = dict(one_candidate, volume="12", pages="4471")

    importer._enrich_from_openalex(rec, "/lib/s41467-021-24715-3.pdf")

    assert asked == []
    assert rec["pages"] == "4471"


def test_a_dead_doi_is_still_reported_unverified(one_candidate, monkeypatch):
    monkeypatch.setattr(importer.metrics, "resolve_doi", lambda d: None)
    monkeypatch.setattr(importer.metrics, "fetch_metrics",
                        lambda d: (None,) * 12)
    rec = one_candidate

    importer._enrich_from_openalex(rec, "/lib/s41467-021-24715-3.pdf")

    assert rec.get("metadata_unverified") is True
