"""Fetching PDB mentions for a whole page of cards at once.

make_card issued one query per card. With the library growing and a
bulk import holding the database busy, those per-card queries were a
measurable part of a multi-second rebuild.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index, pdb_mentions


def _library(tmp_path):
    conn = index.open_db(str(tmp_path / "lib.db"))
    ids = {}
    for name in ("a", "b", "c"):
        pdf = str(tmp_path / (name + ".pdf"))
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF")
        sc = pdf + ".alexandria"
        with open(sc, "w") as fh:
            fh.write('{"title": "%s"}' % name)
        index.upsert(conn, pdf, sc, None, {"title": name},
                     os.path.getmtime(sc))
        ids[name] = conn.execute(
            "SELECT id FROM papers WHERE pdf_path = ?",
            (pdf,)).fetchone()[0]
    return conn, ids


def test_bulk_returns_a_mapping_keyed_by_paper(tmp_path):
    conn, ids = _library(tmp_path)
    pdb_mentions.store_mentions(
        conn, ids["a"], [("1abc", "methods"), ("2def", None)], "test")
    pdb_mentions.store_mentions(conn, ids["b"], [("3ghi", None)], "test")

    got = pdb_mentions.get_pdb_mentions_bulk(
        conn, [ids["a"], ids["b"], ids["c"]])

    assert sorted(m["pdb_id"] for m in got[ids["a"]]) == ["1abc", "2def"]
    assert [m["pdb_id"] for m in got[ids["b"]]] == ["3ghi"]
    assert got.get(ids["c"], []) == []


def test_bulk_matches_the_per_paper_call(tmp_path):
    conn, ids = _library(tmp_path)
    pdb_mentions.store_mentions(
        conn, ids["a"], [("1abc", "methods")], "test")
    one = pdb_mentions.get_pdb_mentions(conn, ids["a"])
    many = pdb_mentions.get_pdb_mentions_bulk(conn, [ids["a"]])[ids["a"]]
    assert [m["pdb_id"] for m in one] == [m["pdb_id"] for m in many]
    assert [m["section"] for m in one] == [m["section"] for m in many]


def test_bulk_is_one_query(tmp_path):
    conn, ids = _library(tmp_path)
    queries = []
    real_execute = conn.execute

    class Counting:
        def execute(self, sql, *a):
            queries.append(sql)
            return real_execute(sql, *a)

    pdb_mentions.get_pdb_mentions_bulk(
        Counting(), list(ids.values()))
    assert len(queries) == 1


def test_bulk_with_no_ids_asks_nothing(tmp_path):
    class Exploding:
        def execute(self, *a):
            raise AssertionError("should not query")

    assert pdb_mentions.get_pdb_mentions_bulk(Exploding(), []) == {}


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ---- the valid-id set is shared, not rebuilt per paper -------------

def test_valid_ids_are_cached_across_calls(tmp_path, monkeypatch):
    """_schedule_pdb_indexing spawns a thread per imported paper, and
    each was building its own 258k-element set from the database. An
    all-threads dump during a 185-PDF import caught ~200 of them in
    get_valid_pdb_ids at once, thrashing the GIL."""
    pdb_mentions.reset_valid_pdb_id_cache()
    conn, _ids = _library(tmp_path)
    pdb_mentions._store_valid_pdb_ids(conn, ["1abc", "2def"])

    queries = []
    real_execute = conn.execute

    class Counting:
        def execute(self, sql, *a):
            queries.append(sql)
            return real_execute(sql, *a)

    first = pdb_mentions.get_valid_pdb_ids(Counting())
    second = pdb_mentions.get_valid_pdb_ids(Counting())
    assert first == second == {"1abc", "2def"}
    assert len(queries) == 1, "queried {} times".format(len(queries))


def test_storing_new_ids_refreshes_the_cache(tmp_path):
    pdb_mentions.reset_valid_pdb_id_cache()
    conn, _ids = _library(tmp_path)
    pdb_mentions._store_valid_pdb_ids(conn, ["1abc"])
    assert pdb_mentions.get_valid_pdb_ids(conn) == {"1abc"}
    pdb_mentions._store_valid_pdb_ids(conn, ["9xyz"])
    assert "9xyz" in pdb_mentions.get_valid_pdb_ids(conn)


# ---- the weekly id-cache refresh must not block an import ---------

def test_stale_cache_refresh_is_scheduled_not_awaited(tmp_path,
                                                      monkeypatch):
    """refresh_valid_pdb_id_cache downloads wwPDB's 57 MB entries.idx
    and parses ~259k lines. It was called from
    index_pdb_mentions_for_paper, so the first import after the
    7-day TTL expired paid for all of it inline, holding the GIL.
    Scheduling must return immediately."""
    conn, _ids = _library(tmp_path)
    started = []

    def fake_worker(db_path):
        started.append(db_path)

    monkeypatch.setattr(pdb_mentions, "_refresh_worker", fake_worker)
    # Empty cache table == stale.
    assert pdb_mentions.schedule_valid_pdb_id_refresh(conn) is True
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started, "refresh worker was never started"


def test_fresh_cache_schedules_nothing(tmp_path, monkeypatch):
    conn, _ids = _library(tmp_path)
    pdb_mentions._store_valid_pdb_ids(conn, ["1abc"])

    def explode(db_path):
        raise AssertionError("should not refresh a fresh cache")

    monkeypatch.setattr(pdb_mentions, "_refresh_worker", explode)
    assert pdb_mentions.schedule_valid_pdb_id_refresh(conn) is False


def test_indexing_a_paper_never_downloads_inline(tmp_path,
                                                 monkeypatch):
    """The import path may schedule a refresh; it must never wait
    for one."""
    conn, ids = _library(tmp_path)
    monkeypatch.setattr(
        pdb_mentions, "refresh_valid_pdb_id_cache",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("blocking refresh called from import")))
    monkeypatch.setattr(pdb_mentions, "_refresh_worker",
                        lambda db_path: None)
    monkeypatch.setattr(pdb_mentions, "_pdf_fulltext",
                        lambda p: "a paper mentioning 1abc")
    monkeypatch.setattr(pdb_mentions, "fetch_pmid_for_doi",
                        lambda doi, timeout=15: None)
    pdb_mentions.index_pdb_mentions_for_paper(conn, ids["a"])


def test_parser_skips_headers_and_keeps_four_char_ids():
    lines = [b"IDCODE\tHEADER\n", b"------\t------\n",
             b"100D\tsomething\n", b"1ABC\tother\n",
             b"TOOLONG\tx\n", b"\n"]
    got = pdb_mentions._parse_entries_idx(lines)
    assert got == ["100d", "1abc"]
