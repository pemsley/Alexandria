"""The sidecar `summary` container (MCP-summaries ladder, step 1):
present-but-None on new records, survives refresh_pdf, and has a
display attribution so a machine summary can never be mistaken for
the abstract."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import importer, index, markup, sidecar

SUMMARY = {
    "text": "Uses a genetic algorithm to dock ligands.",
    "model": "claude-opus-5",
    "source": "jats",
    "generated_at": "2026-08-29T20:00:00Z",
}


def test_new_record_has_summary_container():
    rec = sidecar.new_record("/x/paper.pdf")
    assert "summary" in rec
    assert rec["summary"] is None


def test_attribution_full():
    s = markup.summary_attribution(SUMMARY)
    assert "claude-opus-5" in s
    assert "jats" in s
    assert "2026-08-29" in s


def test_attribution_degrades_when_fields_missing():
    assert markup.summary_attribution({"text": "t"}) == "AI-generated"
    assert markup.summary_attribution(None) == "AI-generated"


def test_refresh_preserves_summary(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    sc = sidecar.sidecar_path_for(pdf)
    rec = sidecar.new_record(pdf)
    rec.update({"title": "T", "doi": "10.1/x", "summary": SUMMARY})
    sidecar.write(sc, rec)
    conn = index.open_db(str(tmp_path / "lib.db"))
    index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))

    monkeypatch.setattr(
        importer, "_build_record",
        lambda p: {"title": "T", "authors": ["A"], "year": 2020,
                   "journal": "J", "doi": "10.1/x"})
    monkeypatch.setattr(
        importer.metrics, "fetch_metrics",
        lambda d: (None,) * 12)
    monkeypatch.setattr(
        importer.metrics, "is_preprint_doi", lambda d: False)
    monkeypatch.setattr(
        importer.thumbnail, "make_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(
        importer, "_schedule_pdb_indexing", lambda *a: None)
    monkeypatch.setattr(
        importer.jats, "fetch_and_store",
        lambda p, d: {"status": "no_pmcid", "pmcid": None,
                      "checked": "2026-08-29"})

    importer.refresh_pdf(conn, pdf)
    assert sidecar.read(sc)["summary"] == SUMMARY


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
