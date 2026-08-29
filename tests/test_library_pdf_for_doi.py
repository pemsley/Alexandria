"""author_works._library_pdf_for_doi — resolve a work-row DOI to the
local (pdf_path, sidecar_path) pair, so in-library rows in the
Authors window can open the actual PDF. Ghost rows (metadata-only,
no real PDF on disk) must not resolve."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import author_works, index, sidecar


def _add(conn, tmp_path, name, doi, real_pdf=True):
    pdf = str(tmp_path / (name + ".pdf"))
    if real_pdf:
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF fake")
    else:
        pdf = "bibtex:" + name
    sc = str(tmp_path / (name + ".alexandria"))
    rec = {"title": name, "authors": ["A"], "year": 2020,
           "journal": "J", "doi": doi}
    sidecar.write(sc, rec)
    index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))
    return pdf, sc


def test_found_returns_pdf_and_sidecar(tmp_path):
    conn = index.open_db(str(tmp_path / "lib.db"))
    pdf, sc = _add(conn, tmp_path, "a", "10.1/abc")
    assert author_works._library_pdf_for_doi(conn, "10.1/abc") == (pdf, sc)


def test_lookup_normalizes_both_sides(tmp_path):
    conn = index.open_db(str(tmp_path / "lib.db"))
    pdf, sc = _add(conn, tmp_path, "a", "https://doi.org/10.1/AbC")
    assert author_works._library_pdf_for_doi(
        conn, "10.1/abc") == (pdf, sc)


def test_ghost_row_does_not_resolve(tmp_path):
    conn = index.open_db(str(tmp_path / "lib.db"))
    _add(conn, tmp_path, "g", "10.1/ghost", real_pdf=False)
    assert author_works._library_pdf_for_doi(conn, "10.1/ghost") is None


def test_absent_doi_returns_none(tmp_path):
    conn = index.open_db(str(tmp_path / "lib.db"))
    assert author_works._library_pdf_for_doi(conn, "10.1/nope") is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
