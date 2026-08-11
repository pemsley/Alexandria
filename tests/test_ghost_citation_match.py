"""Tests for index.find_ghost_by_citation — the no-DOI fallback that
lets a PDF whose text yields no DOI (pre-DOI-era papers) still merge
with its BibTeX ghost. Pure sqlite, no network/GTK.

Runnable as `python3 -m tests.test_ghost_citation_match` or pytest.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index


def _conn(tmp_path):
    return index.open_db(str(tmp_path / "test.db"))


def _plant(conn, pdf_path, title, year=None, authors=None):
    conn.execute(
        "INSERT INTO papers (pdf_path, sidecar_path, title, year,"
        " authors_json) VALUES (?, ?, ?, ?, ?)",
        (pdf_path, pdf_path + ".alexandria", title, year,
         json.dumps(authors or [])))
    conn.commit()


CLUSTAL = ("The CLUSTAL_X windows interface: flexible strategies for "
           "multiple sequence alignment aided by quality analysis tools")


def test_matches_ghost_by_title_year_surname(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:thompson1997the", CLUSTAL, 1997,
           ["Julie Thompson"])
    row = index.find_ghost_by_citation(
        conn, CLUSTAL, 1997,
        ["Julie D. Thompson", "Toby J. Gibson"])
    assert row and row["pdf_path"] == "bibtex:thompson1997the"


def test_title_normalization_is_forgiving(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", "The CLUSTAL X Windows Interface -- "
           "flexible strategies", 1997, ["J Thompson"])
    row = index.find_ghost_by_citation(
        conn, "the clustal_x windows interface: flexible strategies",
        1997, ["Julie Thompson"])
    assert row and row["pdf_path"] == "bibtex:g1"


def test_never_matches_real_pdf_rows(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "/library/some.pdf", CLUSTAL, 1997, ["Julie Thompson"])
    assert index.find_ghost_by_citation(
        conn, CLUSTAL, 1997, ["Julie Thompson"]) is None


def test_year_conflict_rejects(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", CLUSTAL, 1994, ["Julie Thompson"])
    assert index.find_ghost_by_citation(
        conn, CLUSTAL, 1997, ["Julie Thompson"]) is None


def test_ghost_without_year_still_matches(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", CLUSTAL, None, ["Julie Thompson"])
    row = index.find_ghost_by_citation(
        conn, CLUSTAL, 1997, ["Julie D. Thompson"])
    assert row and row["pdf_path"] == "bibtex:g1"


def test_surname_conflict_rejects(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", CLUSTAL, 1997, ["Ann Kowalczyk"])
    assert index.find_ghost_by_citation(
        conn, CLUSTAL, 1997, ["Julie Thompson"]) is None


def test_missing_authors_on_either_side_ok(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", CLUSTAL, 1997, [])
    row = index.find_ghost_by_citation(
        conn, CLUSTAL, 1997, ["Julie Thompson"])
    assert row and row["pdf_path"] == "bibtex:g1"


def test_ambiguous_two_ghosts_declines(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", CLUSTAL, 1997, [])
    _plant(conn, "bibtex:g2", CLUSTAL, 1997, [])
    assert index.find_ghost_by_citation(
        conn, CLUSTAL, 1997, ["Julie Thompson"]) is None


def test_empty_title_never_matches(tmp_path):
    conn = _conn(tmp_path)
    _plant(conn, "bibtex:g1", "", 1997, [])
    assert index.find_ghost_by_citation(conn, "", 1997, []) is None
    assert index.find_ghost_by_citation(conn, None, 1997, []) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
