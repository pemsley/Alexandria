"""Fetching PDB mentions for a whole page of cards at once.

make_card issued one query per card. With the library growing and a
bulk import holding the database busy, those per-card queries were a
measurable part of a multi-second rebuild.
"""

import os
import sys

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
