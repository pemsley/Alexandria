"""jats.backfill walks DOI-bearing library rows, fetches where an
attempt is due, records the outcome in each sidecar, and reports
progress and totals. Fetches are monkeypatched; DB and sidecars are
real (temp dir)."""

import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index, jats, sidecar


def _add_paper(conn, tmp_path, name, doi, extra=None):
    pdf = str(tmp_path / (name + ".pdf"))
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    sc = sidecar.sidecar_path_for(pdf)
    rec = {"title": name, "authors": ["A"], "year": 2020,
           "journal": "J", "doi": doi}
    if extra:
        rec.update(extra)
    sidecar.write(sc, rec)
    index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))
    return pdf, sc


def test_backfill_fetches_updates_and_counts(tmp_path, monkeypatch):
    conn = index.open_db(str(tmp_path / "lib.db"))
    pdf_a, sc_a = _add_paper(conn, tmp_path, "a", "10.1/a")
    pdf_b, sc_b = _add_paper(conn, tmp_path, "b", "10.1/b")
    # c has no DOI: not a candidate at all.
    _add_paper(conn, tmp_path, "c", None)
    # d was checked recently with a negative answer: skipped.
    _add_paper(conn, tmp_path, "d", "10.1/d", extra={
        "jats": {"status": "no_fulltext", "pmcid": "PMC9",
                 "checked": jats._today_iso()}})

    def fake_fetch(pdf_path, doi):
        status = "stored" if doi == "10.1/a" else "no_pmcid"
        return {"status": status, "pmcid": None,
                "checked": jats._today_iso()}

    monkeypatch.setattr(jats, "fetch_and_store", fake_fetch)

    seen = []
    totals = jats.backfill(
        conn, on_progress=lambda done, total: seen.append((done, total)))

    assert totals == {"stored": 1, "absent": 1, "errors": 0,
                      "skipped": 1}
    assert sidecar.read(sc_a)["jats"]["status"] == "stored"
    assert sidecar.read(sc_b)["jats"]["status"] == "no_pmcid"
    # Progress over the DOI-bearing candidates (a, b, d).
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_backfill_stop_event_aborts_early(tmp_path, monkeypatch):
    conn = index.open_db(str(tmp_path / "lib.db"))
    for i in range(3):
        _add_paper(conn, tmp_path, "p{}".format(i), "10.1/{}".format(i))

    stop = threading.Event()
    calls = []

    def fake_fetch(pdf_path, doi):
        calls.append(doi)
        stop.set()   # ask to stop after the first fetch
        return {"status": "no_pmcid", "pmcid": None,
                "checked": jats._today_iso()}

    monkeypatch.setattr(jats, "fetch_and_store", fake_fetch)
    jats.backfill(conn, stop=stop)
    assert len(calls) == 1


def test_backfill_skips_rows_without_a_real_pdf(tmp_path, monkeypatch):
    """BibTeX ghosts carry pseudo pdf_paths ('bibtex:<key>'); JATS
    belongs beside a real PDF, so those rows must be skipped, never
    fetched."""
    conn = index.open_db(str(tmp_path / "lib.db"))
    # A ghost: sidecar exists, pdf_path is not a file on disk.
    sc = str(tmp_path / "ghost.alexandria")
    rec = {"title": "G", "authors": ["A"], "year": 2020,
           "journal": "J", "doi": "10.1/ghost"}
    sidecar.write(sc, rec)
    index.upsert(conn, "bibtex:ghost2020key", sc, None, rec,
                 os.path.getmtime(sc))

    def explode(pdf_path, doi):
        raise AssertionError("fetched for a ghost row")

    monkeypatch.setattr(jats, "fetch_and_store", explode)
    totals = jats.backfill(conn)
    assert totals == {"stored": 0, "absent": 0, "errors": 0,
                      "skipped": 1}


def test_backfill_error_status_counts_as_error(tmp_path, monkeypatch):
    conn = index.open_db(str(tmp_path / "lib.db"))
    _add_paper(conn, tmp_path, "a", "10.1/a")
    monkeypatch.setattr(
        jats, "fetch_and_store",
        lambda p, d: {"status": "error", "pmcid": None,
                      "checked": jats._today_iso()})
    totals = jats.backfill(conn)
    assert totals["errors"] == 1
    assert totals["stored"] == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
