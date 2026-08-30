"""One dropped PDF must be extracted once, not three times.

A drop lands the file in the library and imports it; the watcher's
own CREATED and CHANGES_DONE_HINT events fire imports for the same
path at the same moment. Each pass runs `_build_record` (pdfx /
pypdf — pure Python, holds the GIL), so three concurrent passes
starve the GTK main loop for tens of seconds. Concurrent imports
of the same path must collapse to one extraction.
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import importer, index, sidecar


def _quiet(monkeypatch, extract_calls, delay=0.3):
    def slow_build(pdf_path):
        extract_calls.append(pdf_path)
        time.sleep(delay)          # stand-in for pdfx's seconds
        return {"title": "T", "authors": ["A"], "year": 2020,
                "journal": "J", "doi": None}

    monkeypatch.setattr(importer, "_build_record", slow_build)
    monkeypatch.setattr(
        importer.metrics, "fetch_metrics", lambda d: (None,) * 12)
    monkeypatch.setattr(
        importer.metrics, "is_preprint_doi", lambda d: False)
    monkeypatch.setattr(
        importer.thumbnail, "make_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(
        importer, "_schedule_pdb_indexing", lambda *a: None)
    monkeypatch.setattr(
        importer.jats, "fetch_and_store",
        lambda p, d: {"status": "no_pmcid", "pmcid": None,
                      "checked": "2026-08-30"})


def _pdf(tmp_path, name="paper.pdf"):
    p = str(tmp_path / name)
    with open(p, "wb") as fh:
        fh.write(b"%PDF fake " + name.encode())
    return p


def _run_concurrently(fns):
    threads = [threading.Thread(target=f) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)


def test_concurrent_imports_of_one_path_extract_once(
        tmp_path, monkeypatch):
    calls = []
    _quiet(monkeypatch, calls)
    pdf = _pdf(tmp_path)
    results = []
    lock = threading.Lock()
    # Create the DB up front; the app's importers always attach to an
    # existing one (watcher via connect_existing, browser via its own
    # long-lived handle).
    db_path = str(tmp_path / "lib.db")
    index.open_db(db_path).close()

    def do_import():
        conn = index.connect_existing(db_path)
        try:
            out = importer.import_pdf(conn, pdf)
        finally:
            conn.close()
        with lock:
            results.append(out)

    _run_concurrently([do_import, do_import, do_import])

    assert len(calls) == 1, "extracted {} times".format(len(calls))
    assert len(results) == 3
    assert os.path.isfile(sidecar.sidecar_path_for(pdf))


def test_second_import_still_returns_a_usable_status(
        tmp_path, monkeypatch):
    calls = []
    _quiet(monkeypatch, calls)
    pdf = _pdf(tmp_path)
    conn = index.open_db(str(tmp_path / "lib.db"))
    try:
        _rec, first = importer.import_pdf(conn, pdf)
        _rec2, second = importer.import_pdf(conn, pdf)
    finally:
        conn.close()
    assert first == "new"
    # Sequential re-import is the pre-existing recent/existing path.
    assert second in ("recent", "existing")


def test_different_paths_both_import(tmp_path, monkeypatch):
    calls = []
    _quiet(monkeypatch, calls, delay=0.5)
    a = _pdf(tmp_path, "a.pdf")
    b = _pdf(tmp_path, "b.pdf")

    db_path = str(tmp_path / "lib.db")
    index.open_db(db_path).close()

    def imp(p):
        def go():
            conn = index.connect_existing(db_path)
            try:
                importer.import_pdf(conn, p)
            finally:
                conn.close()
        return go

    t0 = time.monotonic()
    _run_concurrently([imp(a), imp(b)])
    elapsed = time.monotonic() - t0

    # Both files are extracted — the in-flight guard collapses
    # repeats of ONE path, never distinct ones.
    assert len(calls) == 2
    # They no longer overlap: extraction is deliberately serialised
    # (see test_extraction_is_serialised), so this is ~2x the single
    # extraction time rather than 1x. The bound catches a deadlock,
    # not a slow machine.
    assert elapsed < 5.0, "took {:.2f}s — deadlock?".format(elapsed)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


def test_extraction_is_serialised(tmp_path, monkeypatch):
    """Three producers reach extraction at once during a bulk
    import — _run_import, the watcher's per-file threads, and the
    startup reconcile. _run_pdfx is pure Python, so running them
    concurrently just splits the GIL three ways and each takes
    three times as long. Serialise it.
    """
    concurrent = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def watching_build(pdf_path):
        with lock:
            concurrent["now"] += 1
            concurrent["peak"] = max(concurrent["peak"],
                                     concurrent["now"])
        time.sleep(0.15)
        with lock:
            concurrent["now"] -= 1
        return {"title": "T", "authors": ["A"], "year": 2020,
                "journal": "J", "doi": None}

    monkeypatch.setattr(importer, "_build_record", watching_build)
    monkeypatch.setattr(
        importer.metrics, "fetch_metrics", lambda d: (None,) * 12)
    monkeypatch.setattr(
        importer.metrics, "is_preprint_doi", lambda d: False)
    monkeypatch.setattr(
        importer.thumbnail, "make_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(importer, "_schedule_pdb_indexing",
                        lambda *a: None)
    monkeypatch.setattr(
        importer.jats, "fetch_and_store",
        lambda p, d: {"status": "no_pmcid", "pmcid": None,
                      "checked": "2026-08-30"})

    db_path = str(tmp_path / "lib.db")
    index.open_db(db_path).close()
    pdfs = [_pdf(tmp_path, "p{}.pdf".format(i)) for i in range(4)]

    def imp(path):
        def go():
            conn = index.connect_existing(db_path)
            try:
                importer.import_pdf(conn, path)
            finally:
                conn.close()
        return go

    _run_concurrently([imp(p) for p in pdfs])
    assert concurrent["peak"] == 1, \
        "{} extractions ran at once".format(concurrent["peak"])
