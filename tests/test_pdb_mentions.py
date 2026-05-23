"""Tests for PDB-mention index tables in alexandria.index.

Runnable as `python3 -m tests.test_pdb_mentions` (no pytest
required) or collectable by pytest.
"""

import os
import sys
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index
from alexandria import pdb_mentions


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(index.CREATE_TABLE)      # papers table
    index.create_pdb_tables(conn)               # new
    return conn


def test_pdb_tables_exist():
    conn = _mem_db()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pdb_mentions", "doi_pmid_cache", "pdb_id_cache"} <= names


def test_extract_pdb_ids_basic():
    valid = {"4hhb", "1a3n", "2hhb"}
    text = "We used 4HHB and 1a3n; see also 2HHB. Year 2023 and word ABCD."
    assert pdb_mentions.extract_pdb_ids_from_text(text, valid) == {
        "4hhb", "1a3n", "2hhb"}

def test_extract_rejects_all_digits_and_non_alpha():
    valid = {"1234"}
    assert pdb_mentions.extract_pdb_ids_from_text("1234 5678", valid) == set()

def test_extract_requires_validation():
    assert pdb_mentions.extract_pdb_ids_from_text("9zzz", {"4hhb"}) == set()

def test_extract_empty_inputs():
    assert pdb_mentions.extract_pdb_ids_from_text("", {"4hhb"}) == set()
    assert pdb_mentions.extract_pdb_ids_from_text("4hhb", set()) == set()


def test_parse_europepmc_pdb_only():
    payload = [{
        "extId": "30425980",
        "annotations": [
            {"exact": "4HHB", "type": "Accession Numbers", "section": "Methods",
             "tags": [{"name": "pdb", "uri": "x"}]},
            {"exact": "P69905", "type": "Accession Numbers", "section": "Methods",
             "tags": [{"name": "uniprot", "uri": "x"}]},
            {"exact": "1A3N", "type": "Accession Numbers", "section": "Results",
             "tags": [{"name": "PDB", "uri": "y"}]},
        ],
    }]
    got = pdb_mentions.parse_europepmc_annotations(payload)
    assert sorted(got) == [("30425980", "1a3n", "results"),
                           ("30425980", "4hhb", "methods")]

def test_parse_europepmc_empty():
    assert pdb_mentions.parse_europepmc_annotations([]) == []
    assert pdb_mentions.parse_europepmc_annotations(None) == []


def test_parse_pmid_hit():
    data = {"resultList": {"result": [{"pmid": "30425980", "title": "x"}]}}
    assert pdb_mentions.parse_pmid_from_search(data) == "30425980"

def test_parse_pmid_no_result():
    assert pdb_mentions.parse_pmid_from_search({"resultList": {"result": []}}) is None
    assert pdb_mentions.parse_pmid_from_search({}) is None
    assert pdb_mentions.parse_pmid_from_search(None) is None

def test_parse_pmid_result_without_pmid():
    data = {"resultList": {"result": [{"id": "PPR123", "source": "PPR"}]}}
    assert pdb_mentions.parse_pmid_from_search(data) is None


def test_pmid_cache_roundtrip():
    conn = _mem_db()
    assert pdb_mentions._get_cached_pmid(conn, "10.1/x") == (False, None)
    pdb_mentions._cache_pmid(conn, "10.1/x", "12345")
    assert pdb_mentions._get_cached_pmid(conn, "10.1/x") == (True, "12345")

def test_pmid_cache_negative():
    conn = _mem_db()
    pdb_mentions._cache_pmid(conn, "10.1/none", None)
    assert pdb_mentions._get_cached_pmid(conn, "10.1/none") == (True, None)


def _seed_paper(conn, pdf_path="/x/a.pdf"):
    cur = conn.execute(
        "INSERT INTO papers (pdf_path, sidecar_path, added_date) "
        "VALUES (?, ?, ?)",
        (pdf_path, pdf_path + ".alexandria", "2026-01-01"))
    conn.commit()
    return cur.lastrowid

def test_store_and_get_mentions():
    conn = _mem_db()
    pid = _seed_paper(conn)
    pdb_mentions.store_mentions(
        conn, pid, [("4hhb", "methods"), ("1a3n", None)], source="europepmc")
    got = pdb_mentions.get_pdb_mentions(conn, pid)
    ids = sorted(m["pdb_id"] for m in got)
    assert ids == ["1a3n", "4hhb"]
    assert all(m["source"] == "europepmc" for m in got)

def test_get_papers_for_pdb_id_case_insensitive():
    conn = _mem_db()
    pid = _seed_paper(conn)
    pdb_mentions.store_mentions(conn, pid, [("4hhb", None)], source="europepmc")
    assert pdb_mentions.get_papers_for_pdb_id(conn, "4HHB") == [pid]
    assert pdb_mentions.get_papers_for_pdb_id(conn, "9zzz") == []

def test_store_mentions_idempotent():
    conn = _mem_db()
    pid = _seed_paper(conn)
    pdb_mentions.store_mentions(conn, pid, [("4hhb", "methods")], "europepmc")
    pdb_mentions.store_mentions(conn, pid, [("4hhb", "methods")], "europepmc")
    assert len(pdb_mentions.get_pdb_mentions(conn, pid)) == 1


def test_valid_id_cache_set_get():
    conn = _mem_db()
    assert pdb_mentions.get_valid_pdb_ids(conn) == set()
    pdb_mentions._store_valid_pdb_ids(conn, ["4HHB", "1a3n"])
    assert pdb_mentions.get_valid_pdb_ids(conn) == {"4hhb", "1a3n"}

def test_valid_id_cache_age():
    conn = _mem_db()
    assert pdb_mentions._valid_cache_is_stale(conn, max_age_days=7) is True
    pdb_mentions._store_valid_pdb_ids(conn, ["4HHB"])
    assert pdb_mentions._valid_cache_is_stale(conn, max_age_days=7) is False


def test_index_uses_europepmc_then_skips_regex():
    conn = _mem_db()
    pid = _seed_paper(conn, "/x/withdoi.pdf")
    conn.execute("UPDATE papers SET doi=? WHERE id=?", ("10.1/x", pid))
    conn.commit()
    saved_pmid = pdb_mentions.fetch_pmid_for_doi
    saved_ann = pdb_mentions.fetch_europepmc_annotations
    pdb_mentions.fetch_pmid_for_doi = lambda doi, timeout=15: "999"
    pdb_mentions.fetch_europepmc_annotations = (
        lambda pmids, timeout=30: [("999", "4hhb", "methods")])
    try:
        n = pdb_mentions.index_pdb_mentions_for_paper(conn, pid)
    finally:
        pdb_mentions.fetch_pmid_for_doi = saved_pmid
        pdb_mentions.fetch_europepmc_annotations = saved_ann
    assert n == 1
    got = pdb_mentions.get_pdb_mentions(conn, pid)
    assert got[0]["pdb_id"] == "4hhb"
    assert got[0]["source"] == "europepmc"

def test_index_no_doi_no_text_is_noop():
    conn = _mem_db()
    pid = _seed_paper(conn, "/x/nodoi.pdf")
    n = pdb_mentions.index_pdb_mentions_for_paper(conn, pid)
    assert n == 0
    assert pdb_mentions.get_pdb_mentions(conn, pid) == []


# ---- Self-test runner ---------------------------------------------

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        name = t.__name__
        try:
            t()
        except AssertionError as e:
            failures += 1
            print("FAIL  {}\n        {}".format(name, e))
        except Exception as e:
            failures += 1
            print("ERROR {}\n        {!r}".format(name, e))
        else:
            print("ok    {}".format(name))
    print()
    print("{} test(s), {} failure(s)".format(len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
