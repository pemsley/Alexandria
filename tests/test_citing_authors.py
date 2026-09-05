"""Who cites this author most often.

Asked for 2026-09-01. The backlog design assumed this needed one API
call per work of the author's, then a walk over every citing paper —
tens of calls and tens of thousands of records. It does not: OpenAlex
will OR up to 100 work IDs in a `cites:` filter and tally the citing
papers' authors server-side with `group_by=authorships.author.id`.
Measured against a real author (223 works, 44,000 citing papers): two
calls, one for the work IDs and one for the tally.

Self-citations are excluded in the same request, via
`authorships.author.id:!A<id>`.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index, metrics


def _groups(*pairs):
    """An OpenAlex `group_by` payload."""
    return [{"key": "https://openalex.org/" + k,
             "key_display_name": n, "count": c} for k, n, c in pairs]


# ---- turning a group_by payload into a ranked list ------------------

def test_citing_authors_are_ranked_by_count():
    got = metrics.rank_citing_authors(
        _groups(("A2", "Beta", 40), ("A1", "Alpha", 90),
                ("A3", "Gamma", 65)), limit=10)
    assert [a["name"] for a in got] == ["Alpha", "Gamma", "Beta"]
    assert [a["count"] for a in got] == [90, 65, 40]


def test_the_openalex_id_is_bare_not_a_url():
    """The rest of the app passes around `A123`, not the full URL —
    `author_works.open_window` and the trail both key on it."""
    got = metrics.rank_citing_authors(_groups(("A1", "Alpha", 5)), limit=5)
    assert got[0]["openalex_id"] == "A1"


def test_the_limit_is_honoured():
    got = metrics.rank_citing_authors(
        _groups(*[("A%d" % i, "N%d" % i, i) for i in range(30)]), limit=12)
    assert len(got) == 12


def test_the_author_is_never_in_their_own_list():
    """Belt and braces: the API call excludes self-citations, but a
    stale or hand-built payload must not put the author top of the
    list of people who cite them."""
    got = metrics.rank_citing_authors(
        _groups(("A1", "Alpha", 900), ("A2", "Beta", 40)),
        limit=10, exclude_id="A1")
    assert [a["name"] for a in got] == ["Beta"]


def test_unknown_and_empty_groups_are_dropped():
    """OpenAlex emits an `unknown` bucket for works whose authorships
    it could not resolve."""
    groups = _groups(("A1", "Alpha", 10))
    groups.append({"key": "unknown", "key_display_name": "unknown",
                   "count": 500})
    groups.append({"key": "https://openalex.org/A9",
                   "key_display_name": "", "count": 7})
    got = metrics.rank_citing_authors(groups, limit=10)
    assert [a["name"] for a in got] == ["Alpha"]


def test_a_zero_count_is_not_a_citation():
    got = metrics.rank_citing_authors(
        _groups(("A1", "Alpha", 0), ("A2", "Beta", 3)), limit=10)
    assert [a["name"] for a in got] == ["Beta"]


def test_an_empty_payload_is_an_empty_list():
    assert metrics.rank_citing_authors([], limit=10) == []
    assert metrics.rank_citing_authors(None, limit=10) == []


# ---- the request the tally is built from ---------------------------

def test_the_filter_ors_the_works_and_excludes_the_author():
    url = metrics.citing_authors_url(["W1", "W2", "W3"], "A7")
    assert "cites:W1|W2|W3" in url
    assert "authorships.author.id:!A7" in url
    assert "group_by=authorships.author.id" in url


def test_no_more_works_are_asked_for_than_openalex_will_take():
    """100 OR'd IDs is accepted; 200 is rejected outright (measured
    2026-09-01), so the caller must cap rather than discover this as
    an empty result."""
    assert metrics.MAX_CITES_OR_IDS <= 100
    url = metrics.citing_authors_url(
        ["W%d" % i for i in range(500)], "A7")
    assert url.count("|") == metrics.MAX_CITES_OR_IDS - 1


def test_no_works_means_no_request():
    assert metrics.citing_authors_url([], "A7") is None


# ---- the cache -------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    return index.open_db(str(tmp_path / "t.db"))


def test_round_trip(conn):
    top = [{"openalex_id": "A1", "name": "Alpha", "count": 9}]
    index.set_author_relations(conn, "A7", top)
    got = index.get_author_relations(conn, "A7")
    assert got["cited_by_top"] == top
    assert got["computed_at"]


def test_absent_is_none(conn):
    assert index.get_author_relations(conn, "A7") is None
    assert index.get_author_relations(conn, None) is None


def test_a_second_write_replaces_the_first(conn):
    index.set_author_relations(conn, "A7", [{"openalex_id": "A1",
                                             "name": "Alpha",
                                             "count": 1}])
    index.set_author_relations(conn, "A7", [{"openalex_id": "A2",
                                             "name": "Beta",
                                             "count": 2}])
    got = index.get_author_relations(conn, "A7")
    assert [a["name"] for a in got["cited_by_top"]] == ["Beta"]


def test_an_empty_result_is_stored_not_discarded(conn):
    """An author nobody cites is a real answer. Without a row we
    would re-ask OpenAlex on every open of their page."""
    index.set_author_relations(conn, "A7", [])
    got = index.get_author_relations(conn, "A7")
    assert got is not None and got["cited_by_top"] == []


def test_freshness_is_judged_against_the_ttl(conn):
    index.set_author_relations(conn, "A7", [])
    assert index.author_relations_fresh(index.get_author_relations(conn, "A7"))
    stale = {"computed_at": "2001-01-01T00:00:00", "cited_by_top": []}
    assert not index.author_relations_fresh(stale)
    assert not index.author_relations_fresh(None)


def test_corrupt_json_degrades_to_no_cache(conn):
    conn.execute("INSERT INTO author_relations "
                 "(openalex_id, cited_by_top_json, computed_at) "
                 "VALUES ('A7', '{not json', '2026-09-01T00:00:00')")
    conn.commit()
    assert index.get_author_relations(conn, "A7") is None


# ---- a worker thread finding its own connection --------------------

def test_a_connection_can_say_where_it_lives(conn, tmp_path):
    """The author page is handed a connection and no path, but its
    background worker must open its own — sharing the GUI's is the
    fault that segfaulted on macOS."""
    assert index.db_path_of(conn) == str(tmp_path / "t.db")


def test_an_in_memory_connection_has_no_path():
    import sqlite3
    assert index.db_path_of(sqlite3.connect(":memory:")) is None


def test_a_closed_connection_does_not_raise():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.close()
    assert index.db_path_of(c) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
