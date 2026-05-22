"""Tests for PDB-mention index tables in pdforg.index.

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

from pdforg import index
from pdforg import pdb_mentions


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
