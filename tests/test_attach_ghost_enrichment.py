"""attach_pdf_to_ghost: when the attached PDF's import found no DOI,
the ghost's DOI must be adopted AND enrichment must run on it —
without shrinking the PDF-extracted author list (the CLUSTAL_X
story: five-author 1997 paper, OpenAlex record lists one author).

import_pdf and fetch_metrics are monkeypatched — no network, no
poppler.  Runnable via pytest.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import bibtex_import, importer, index, metrics, sidecar

DOI = "10.1093/nar/25.24.4876"
EXTRACTED = ["Julie D. Thompson", "Toby J. Gibson",
             "F. Plewniak", "F. Jeanmougin"]


def test_merge_backfills_doi_and_enriches(tmp_path, monkeypatch):
    root = str(tmp_path)
    conn = index.open_db(os.path.join(root, "db.sqlite3"))

    # Plant the ghost: sidecar in .alexandria-bibtex + index row.
    ghost_rec = sidecar.new_record(sidecar.ghost_pdf_path("thompson1997the"))
    ghost_rec.update({"title": "The CLUSTAL_X windows interface",
                      "authors": ["Julie Thompson"], "year": "1997",
                      "doi": DOI, "bibtex_key": "thompson1997the"})
    g_sc = sidecar.ghost_sidecar_path(root, "thompson1997the")
    os.makedirs(os.path.dirname(g_sc), exist_ok=True)
    sidecar.write(g_sc, ghost_rec)
    index.upsert(conn, "bibtex:thompson1997the", g_sc, None,
                 ghost_rec, os.path.getmtime(g_sc))
    ghost_row = dict(conn.execute(
        "SELECT * FROM papers WHERE pdf_path='bibtex:thompson1997the'"
    ).fetchone())

    # The "downloaded PDF" — content never parsed (import mocked;
    # the DOI-gate scan failing on junk is the production behavior
    # for text-less scans and returns ok).
    src = os.path.join(root, "25-24-4876.pdf")
    with open(src, "wb") as f:
        f.write(b"%PDF-1.4 junk")

    def fake_import(conn_, pdf_path):
        # What import_pdf does for a no-DOI PDF: sidecar with
        # extracted metadata, no enrichment.
        rec = sidecar.new_record(pdf_path)
        rec.update({"title": "The CLUSTAL_X windows interface",
                    "authors": list(EXTRACTED), "year": 1997,
                    "doi": None, "sha256": "x"})
        sc = sidecar.sidecar_path_for(pdf_path)
        sidecar.write(sc, rec)
        index.upsert(conn_, pdf_path, sc, None, rec,
                     os.path.getmtime(sc))
        return rec, "new"

    calls = []

    def fake_fetch(doi):
        calls.append(doi)
        aships = [{"name": "Julie Thompson", "position": "first",
                   "orcid": None, "openalex_id": "A123",
                   "institution": None, "is_corresponding": False}]
        return (39281, "openalex", ["alignment"], "abstract…",
                aships, [], "The CLUSTAL_X windows interface", 1997,
                True, "bronze", [], [])

    monkeypatch.setattr(bibtex_import.importer, "import_pdf",
                        fake_import)
    monkeypatch.setattr(bibtex_import.metrics, "fetch_metrics",
                        fake_fetch)

    new_path, status, msg = bibtex_import.attach_pdf_to_ghost(
        conn, ghost_row, src, root)

    assert status == "merged", msg
    assert new_path.endswith("thompson1997the.pdf")
    merged = sidecar.read(sidecar.sidecar_path_for(new_path))
    assert merged["doi"] == DOI                 # backfilled
    assert calls == [DOI]                       # enrichment ran, once
    assert merged["authors"] == EXTRACTED       # never shrunk
    assert len(merged["authorships"]) == 1      # structured data stored
    assert merged["citations"] == 39281
    assert merged["bibtex_key"] == "thompson1997the"
    # Ghost is gone from the index.
    assert conn.execute(
        "SELECT COUNT(*) FROM papers WHERE pdf_path LIKE 'bibtex:%'"
    ).fetchone()[0] == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
