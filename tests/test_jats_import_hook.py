"""import_pdf fetches JATS full text for new DOI-bearing PDFs.

Everything heavy is monkeypatched (metadata extraction, OpenAlex
enrichment, thumbnails, PDB indexing, and the JATS fetch itself);
the test asserts the wiring: the fetch is attempted exactly for
records with a DOI, and its result lands in the sidecar's `jats`
block.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import importer, index, sidecar


def _quiet_import_deps(monkeypatch, doi):
    monkeypatch.setattr(
        importer, "_build_record",
        lambda p: {"title": "T", "authors": ["A"], "year": 2020,
                   "journal": "J", "doi": doi})
    monkeypatch.setattr(
        importer.metrics, "fetch_metrics",
        lambda d: (None, None, None, None, None, None,
                   None, None, None, None, None, None))
    monkeypatch.setattr(
        importer.metrics, "is_preprint_doi", lambda d: False)
    monkeypatch.setattr(
        importer.thumbnail, "make_thumbnail",
        lambda *a, **k: None)
    monkeypatch.setattr(
        importer, "_schedule_pdb_indexing", lambda *a: None)


def test_new_pdf_with_doi_gets_jats_block(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF-1.4 fake")
    conn = index.open_db(str(tmp_path / "lib.db"))
    _quiet_import_deps(monkeypatch, doi="10.1000/x")

    calls = []

    def fake_fetch(pdf_path, doi):
        calls.append((pdf_path, doi))
        return {"status": "stored", "pmcid": "PMC1",
                "checked": "2026-08-28"}

    monkeypatch.setattr(importer.jats, "fetch_and_store", fake_fetch)

    rec, status = importer.import_pdf(conn, pdf)

    assert status == "new"
    assert calls == [(pdf, "10.1000/x")]
    on_disk = sidecar.read(sidecar.sidecar_path_for(pdf))
    assert on_disk["jats"] == {"status": "stored", "pmcid": "PMC1",
                               "checked": "2026-08-28"}


def test_new_pdf_without_doi_skips_jats(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF-1.4 fake")
    conn = index.open_db(str(tmp_path / "lib.db"))
    _quiet_import_deps(monkeypatch, doi=None)

    def explode(pdf_path, doi):
        raise AssertionError("fetch_and_store called without DOI")

    monkeypatch.setattr(importer.jats, "fetch_and_store", explode)

    rec, status = importer.import_pdf(conn, pdf)
    assert status == "new"
    assert "jats" not in sidecar.read(sidecar.sidecar_path_for(pdf))


def test_jats_failure_never_blocks_import(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF-1.4 fake")
    conn = index.open_db(str(tmp_path / "lib.db"))
    _quiet_import_deps(monkeypatch, doi="10.1000/x")

    def boom(pdf_path, doi):
        raise RuntimeError("unexpected crash inside jats")

    monkeypatch.setattr(importer.jats, "fetch_and_store", boom)

    rec, status = importer.import_pdf(conn, pdf)
    assert status == "new"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
