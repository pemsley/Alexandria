"""The authors database is shared by every catalogue on the host.

Before this, each catalogue had its own `author_trail`,
`author_scores`, `author_works_cache` and `author_relations` — so
following someone in one library left no trace in another, and which
answers you got depended on which window you happened to open. None
of those four tables holds anything catalogue-specific: they are all
keyed by a global identity. Only "do I hold this paper" differs
between libraries, and that stays in `main`.
"""

import os
import sqlite3

import pytest

from alexandria import index


def _old_style_db(path, trail_rows=(), scores=()):
    """A catalogue database from before the split: the author tables
    live in `main`, as `open_db` used to create them."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for ddl in (index.CREATE_AUTHOR_TRAIL, index.CREATE_AUTHOR_SCORES,
                index.CREATE_AUTHOR_WORKS_CACHE,
                index.CREATE_AUTHOR_RELATIONS):
        conn.executescript(ddl.replace("authors.", ""))
    for key, name, pos, added in trail_rows:
        conn.execute(
            "INSERT INTO author_trail (key, openalex_id, name, position,"
            " added_at, last_viewed) VALUES (?, ?, ?, ?, ?, ?)",
            (key, key, name, pos, added, added))
    for oa in scores:
        conn.execute(
            "INSERT INTO author_scores (openalex_id, computed_at)"
            " VALUES (?, ?)", (oa, "2026-01-01"))
    conn.commit()
    conn.close()


def test_authors_db_is_outside_any_catalogue(tmp_path):
    path = index.authors_db_path()
    assert path.endswith(".db")
    assert os.path.basename(path).startswith("authors.")
    assert os.path.dirname(path).endswith("Alexandria")
    # per-host, for the same NFS reason the library database is
    assert os.path.basename(path) != "authors.db"


def test_authors_db_path_follows_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere"))
    assert index.authors_db_path().startswith(str(tmp_path / "elsewhere"))


def test_two_catalogues_share_one_trail(tmp_path):
    a = index.open_db(str(tmp_path / "a" / "library.db"))
    b = index.open_db(str(tmp_path / "b" / "library.db"))
    index.add_author_trail(a, {"openalex_id": "A1", "name": "Ada"})
    assert [r["name"] for r in index.list_author_trail(b)] == ["Ada"]
    # ... and removal is shared too
    index.remove_author_trail(b, "A1")
    assert index.list_author_trail(a) == []


def test_papers_stay_per_catalogue(tmp_path):
    """The one thing that genuinely differs must not be shared."""
    a = index.open_db(str(tmp_path / "a" / "library.db"))
    b = index.open_db(str(tmp_path / "b" / "library.db"))
    a.execute("INSERT INTO papers (pdf_path, sidecar_path, title)"
              " VALUES (?, ?, ?)",
              ("/x/one.pdf", "/x/one.alexandria", "One"))
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0


def test_existing_trail_is_merged_in(tmp_path):
    path = str(tmp_path / "library.db")
    _old_style_db(path, trail_rows=[("A1", "Ada", 1, "2026-01-01"),
                                    ("A2", "Grace", 2, "2026-01-02")],
                  scores=["A1"])
    conn = index.open_db(path)
    assert [r["name"] for r in index.list_author_trail(conn)] == \
        ["Ada", "Grace"]
    assert index.get_author_score(conn, "A1") is not None


def test_merged_tables_are_kept_not_dropped(tmp_path):
    """A curated trail is the user's work — keep a copy."""
    path = str(tmp_path / "library.db")
    _old_style_db(path, trail_rows=[("A1", "Ada", 1, "2026-01-01")])
    conn = index.open_db(path)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    assert "author_trail_premerge" in names
    assert "author_trail" not in names   # would shadow authors.author_trail


def test_merge_does_not_resurrect_a_removed_author(tmp_path):
    """The merge must be one-off: reopening the catalogue after the
    user deletes someone from the trail must not bring them back."""
    path = str(tmp_path / "library.db")
    _old_style_db(path, trail_rows=[("A1", "Ada", 1, "2026-01-01"),
                                    ("A2", "Grace", 2, "2026-01-02")])
    conn = index.open_db(path)
    index.remove_author_trail(conn, "A1")
    conn.close()
    conn = index.open_db(path)
    assert [r["name"] for r in index.list_author_trail(conn)] == ["Grace"]


def test_two_trails_merge_and_renumber(tmp_path):
    """Both catalogues numbered their trail from 1; the union has to
    be re-ordered, oldest first."""
    pa, pb = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    _old_style_db(pa, trail_rows=[("A1", "Ada", 1, "2026-03-01"),
                                  ("A2", "Grace", 2, "2026-03-02")])
    _old_style_db(pb, trail_rows=[("A3", "Alan", 1, "2026-01-15")])
    index.open_db(pa)
    conn = index.open_db(pb)
    rows = index.list_author_trail(conn)
    assert [r["name"] for r in rows] == ["Alan", "Ada", "Grace"]
    assert [r["position"] for r in rows] == [1, 2, 3]


def test_stale_scores_join_across_the_two_databases(tmp_path):
    """`stale_author_score_ids` reads papers from the catalogue and
    scores from the shared database — the one query that spans both."""
    conn = index.open_db(str(tmp_path / "library.db"))
    conn.execute(
        "INSERT INTO papers (pdf_path, sidecar_path, title,"
        " authorships_json) VALUES (?, ?, ?, ?)",
        ("/x/one.pdf", "/x/one.alexandria", "One",
         '[{"name": "Ada", "openalex_id": "A1"},'
         ' {"name": "Grace", "openalex_id": "A2"}]'))
    conn.commit()
    assert sorted(index.stale_author_score_ids(conn)) == ["A1", "A2"]
    index.set_author_score(conn, "A1", {"computed_at": "2999-01-01"})
    assert index.stale_author_score_ids(conn) == ["A2"]


def test_photo_and_trail_agree_on_identity(tmp_path):
    """The shared image store and the shared trail are keyed the same
    way, so a photo saved anywhere belongs to the trail row anywhere."""
    from alexandria import author_image
    conn = index.open_db(str(tmp_path / "library.db"))
    a = {"openalex_id": "A1", "name": "Ada"}
    row = index.add_author_trail(conn, a)
    assert os.path.basename(author_image.image_path(a)) == row["key"] + ".png"


def test_one_person_under_two_key_kinds_collapses(tmp_path):
    """`author_trail_key` prefers the OpenAlex ID and falls back to
    the ORCID, so the same person arrives under two keys depending on
    what the caller knew. In one shared trail that shows as a
    duplicate; the OpenAlex-keyed row wins."""
    path = str(tmp_path / "library.db")
    _old_style_db(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO author_trail (key, orcid, name, position, added_at,"
        " last_viewed) VALUES (?, ?, ?, ?, ?, ?)",
        ("0000-0002-9055-9128", "0000-0002-9055-9128", "A. Zawaira", 1,
         "2026-01-01", "2026-02-01"))
    conn.execute(
        "INSERT INTO author_trail (key, openalex_id, orcid, name, position,"
        " added_at, last_viewed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("A5065282611", "A5065282611", "0000-0002-9055-9128", "A. Zawaira",
         2, "2026-03-01", "2026-03-02"))
    conn.commit()
    conn.close()

    conn = index.open_db(path)
    rows = index.list_author_trail(conn)
    assert [r["key"] for r in rows] == ["A5065282611"]
    # neither stamp is lost to the collapse
    assert rows[0]["added_at"] == "2026-01-01"
    assert rows[0]["last_viewed"] == "2026-03-02"


def test_adding_by_openalex_id_absorbs_an_orcid_keyed_row(tmp_path):
    """Going forward, the duplicate is never created in the first
    place: the ORCID-keyed row is upgraded in place."""
    conn = index.open_db(str(tmp_path / "library.db"))
    index.add_author_trail(conn, {"name": "A. Zawaira",
                                  "orcid": "0000-0002-9055-9128"})
    index.add_author_trail(conn, {"name": "A. Zawaira",
                                  "orcid": "0000-0002-9055-9128",
                                  "openalex_id": "A5065282611"})
    rows = index.list_author_trail(conn)
    assert [r["key"] for r in rows] == ["A5065282611"]
    assert rows[0]["orcid"] == "0000-0002-9055-9128"


def test_the_photo_follows_the_collapsed_row(tmp_path):
    """A photo filed under the ORCID must not be orphaned when the
    row is re-keyed to the OpenAlex ID."""
    from alexandria import author_image
    conn = index.open_db(str(tmp_path / "library.db"))
    a_orcid = {"name": "A. Zawaira", "orcid": "0000-0002-9055-9128"}
    index.add_author_trail(conn, a_orcid)
    os.makedirs(author_image.images_dir(), exist_ok=True)
    with open(author_image.image_path(a_orcid), "wb") as f:
        f.write(b"not really a png")

    full = dict(a_orcid, openalex_id="A5065282611")
    index.add_author_trail(conn, full)
    assert os.path.isfile(author_image.image_path(full))
    assert not os.path.exists(author_image.image_path(a_orcid))


def test_orcid_only_caller_arriving_second_joins_the_existing_row(tmp_path):
    """The other order of arrival: the OpenAlex-keyed row exists and
    a caller turns up knowing only the ORCID."""
    conn = index.open_db(str(tmp_path / "library.db"))
    index.add_author_trail(conn, {"name": "A. Zawaira",
                                  "orcid": "0000-0002-9055-9128",
                                  "openalex_id": "A5065282611"})
    row = index.add_author_trail(conn, {"name": "A. Zawaira",
                                        "orcid": "0000-0002-9055-9128"})
    assert row["key"] == "A5065282611"
    assert len(index.list_author_trail(conn)) == 1


def test_reopening_repairs_a_trail_merged_by_an_earlier_build(tmp_path):
    """Dedup runs on attach, not only on the one-off merge, so a
    database left with duplicates by an earlier build is repaired."""
    path = str(tmp_path / "library.db")
    conn = index.open_db(path)
    conn.execute(
        "INSERT INTO authors.author_trail (key, orcid, name, position,"
        " added_at) VALUES (?, ?, ?, ?, ?)",
        ("0000-0002-9055-9128", "0000-0002-9055-9128", "A. Z", 1, "2026-01-01"))
    conn.execute(
        "INSERT INTO authors.author_trail (key, openalex_id, orcid, name,"
        " position, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("A5065282611", "A5065282611", "0000-0002-9055-9128", "A. Z", 2,
         "2026-02-01"))
    conn.commit()
    conn.close()
    conn = index.open_db(path)
    assert [r["key"] for r in index.list_author_trail(conn)] == ["A5065282611"]
