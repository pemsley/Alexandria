"""Tests for the persistent author-trail table in alexandria.index.

Pure sqlite — no GTK, no network. Runnable as
`python3 -m tests.test_author_trail` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index


def _conn(tmp_path):
    return index.open_db(str(tmp_path / "test.db"))


def test_key_prefers_openalex_id():
    assert index.author_trail_key(
        {"openalex_id": "A123", "orcid": "0000-0001"}) == "A123"


def test_key_falls_back_to_orcid():
    assert index.author_trail_key(
        {"openalex_id": None, "orcid": "0000-0001"}) == "0000-0001"


def test_key_none_when_no_identifier():
    assert index.author_trail_key({"name": "J. Smith"}) is None


def test_add_and_list_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    row = index.add_author_trail(conn, {
        "name": "Jane Kowalczyk", "openalex_id": "A1",
        "orcid": "0000-0001", "institution": "MRC LMB"})
    assert row["key"] == "A1"
    assert row["position"] == 1
    rows = index.list_author_trail(conn)
    assert [r["name"] for r in rows] == ["Jane Kowalczyk"]


def test_add_appends_positions_in_order(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.add_author_trail(conn, {"name": "B", "openalex_id": "A2"})
    index.add_author_trail(conn, {"name": "C", "openalex_id": "A3"})
    rows = index.list_author_trail(conn)
    assert [r["key"] for r in rows] == ["A1", "A2", "A3"]
    assert [r["position"] for r in rows] == [1, 2, 3]


def test_add_existing_is_upsert_not_duplicate(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.add_author_trail(conn, {"name": "B", "openalex_id": "A2"})
    # Re-open A1, now with an institution we didn't have before.
    row = index.add_author_trail(conn, {
        "name": "A", "openalex_id": "A1", "institution": "LMB"})
    rows = index.list_author_trail(conn)
    assert len(rows) == 2
    assert row["position"] == 1          # keeps its slot
    assert rows[0]["institution"] == "LMB"   # backfilled


def test_upsert_does_not_blank_existing_fields(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {
        "name": "A", "openalex_id": "A1", "orcid": "0000-0001",
        "institution": "LMB"})
    # Collaborator chips pass orcid=None / institution=None.
    index.add_author_trail(conn, {
        "name": "A", "openalex_id": "A1",
        "orcid": None, "institution": None})
    row = index.list_author_trail(conn)[0]
    assert row["orcid"] == "0000-0001"
    assert row["institution"] == "LMB"


def test_add_without_identifier_returns_none(tmp_path):
    conn = _conn(tmp_path)
    assert index.add_author_trail(conn, {"name": "Nobody"}) is None
    assert index.list_author_trail(conn) == []


def test_remove_deletes_row(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.add_author_trail(conn, {"name": "B", "openalex_id": "A2"})
    index.remove_author_trail(conn, "A1")
    rows = index.list_author_trail(conn)
    assert [r["key"] for r in rows] == ["A2"]


def test_positions_keep_order_after_remove_and_add(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.add_author_trail(conn, {"name": "B", "openalex_id": "A2"})
    index.remove_author_trail(conn, "A1")
    index.add_author_trail(conn, {"name": "C", "openalex_id": "A3"})
    rows = index.list_author_trail(conn)
    # Gaps in position are fine; order must be stable.
    assert [r["key"] for r in rows] == ["A2", "A3"]


def test_touch_sets_last_viewed(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.touch_author_trail(conn, "A1")
    row = index.list_author_trail(conn)[0]
    assert row["last_viewed"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_upsert_replaces_stale_institution(tmp_path):
    # The AuthorsWindow backfill path feeds the OpenAlex profile's
    # current affiliation through add_author_trail: a fresh non-null
    # institution must replace a stale one (COALESCE puts the new
    # value first), while None still leaves it alone.
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {
        "name": "A", "openalex_id": "A1", "institution": "Old Place"})
    index.add_author_trail(conn, {
        "name": "A", "openalex_id": "A1", "institution": "New Place"})
    assert index.list_author_trail(conn)[0]["institution"] == "New Place"


def test_move_author_trail_reorders(tmp_path):
    conn = _conn(tmp_path)
    for i, k in enumerate(("A1", "A2", "A3", "A4")):
        index.add_author_trail(conn, {"name": k, "openalex_id": k})
    index.move_author_trail(conn, "A4", 0)      # drag to top
    assert [r["key"] for r in index.list_author_trail(conn)] == \
        ["A4", "A1", "A2", "A3"]
    index.move_author_trail(conn, "A1", 3)      # drag to bottom
    assert [r["key"] for r in index.list_author_trail(conn)] == \
        ["A4", "A2", "A3", "A1"]
    index.move_author_trail(conn, "A2", 2)      # middle move
    assert [r["key"] for r in index.list_author_trail(conn)] == \
        ["A4", "A3", "A2", "A1"]


def test_move_author_trail_noop_and_unknown(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.add_author_trail(conn, {"name": "B", "openalex_id": "A2"})
    index.move_author_trail(conn, "A1", 0)      # same place
    index.move_author_trail(conn, "ZZ", 0)      # unknown key
    assert [r["key"] for r in index.list_author_trail(conn)] == \
        ["A1", "A2"]


def test_move_author_trail_clamps_index(tmp_path):
    conn = _conn(tmp_path)
    index.add_author_trail(conn, {"name": "A", "openalex_id": "A1"})
    index.add_author_trail(conn, {"name": "B", "openalex_id": "A2"})
    index.move_author_trail(conn, "A1", 99)     # beyond end -> end
    assert [r["key"] for r in index.list_author_trail(conn)] == \
        ["A2", "A1"]
    index.move_author_trail(conn, "A1", -5)     # below 0 -> top
    assert [r["key"] for r in index.list_author_trail(conn)] == \
        ["A1", "A2"]


def test_positions_rewritten_contiguously_after_move(tmp_path):
    conn = _conn(tmp_path)
    for k in ("A1", "A2", "A3"):
        index.add_author_trail(conn, {"name": k, "openalex_id": k})
    index.remove_author_trail(conn, "A2")       # leaves a gap
    index.move_author_trail(conn, "A3", 0)
    assert [r["position"] for r in index.list_author_trail(conn)] == [1, 2]
