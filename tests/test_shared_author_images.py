"""Author photos belong to the author, not to a catalogue.

Noticed 2026-09-02 and felt 2026-09-05, when switching to the
moorhen catalogue made the avatars disappear: 16 images under the
default library root, 4 under moorhen, 0 under testing — three
disjoint sets of what should be one collection.

The filenames were never the problem. They are
`<index.author_trail_key(authorship)>.png` — OpenAlex ID, else ORCID
— a *global* identity, so the same person already has the same
filename in every catalogue on every machine. Only the directory was
wrong.
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import author_image

AUTHOR = {"name": "Paul Emsley", "openalex_id": "A5001", "orcid": None}
OTHER = {"name": "Kevin Cowtan", "openalex_id": "A5002", "orcid": None}


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    return tmp_path


# ---- one store, not one per library -----------------------------------

def test_the_store_is_outside_any_library(xdg):
    d = author_image.images_dir()
    assert str(xdg / "share" / "Alexandria" / "author-images") == d


def test_xdg_data_home_is_honoured_not_hard_coded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    assert author_image.images_dir().startswith(
        str(tmp_path / "elsewhere"))


def test_without_xdg_data_home_it_falls_back_to_the_default(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert author_image.images_dir().endswith(
        os.path.join(".local", "share", "Alexandria", "author-images"))


def test_the_path_no_longer_depends_on_the_catalogue(xdg, monkeypatch):
    """The bug, stated as a test: two different library roots must
    give one path for one author."""
    a = author_image.image_path(AUTHOR)
    monkeypatch.setattr(author_image.prefs, "get_library_root",
                        lambda: "/somewhere/else")
    assert author_image.image_path(AUTHOR) == a


def test_the_filename_is_still_the_global_identity(xdg):
    assert os.path.basename(author_image.image_path(AUTHOR)) == "A5001.png"


def test_an_author_with_no_identifier_still_has_no_path(xdg):
    assert author_image.image_path({"name": "Nobody"}) is None


# ---- moving what is already there -------------------------------------

def _make(root, key, content=b"png"):
    d = os.path.join(root, author_image.IMAGE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, key + ".png")
    with open(p, "wb") as fh:
        fh.write(content)
    return p


def test_images_move_out_of_every_library(xdg, tmp_path):
    one, two = str(tmp_path / "lib1"), str(tmp_path / "lib2")
    _make(one, "A5001")
    _make(two, "A5002")
    moved = author_image.migrate_into_shared_store([one, two])
    assert moved == 2
    for key in ("A5001", "A5002"):
        assert os.path.isfile(
            os.path.join(author_image.images_dir(), key + ".png"))


def test_the_same_author_in_two_libraries_is_not_duplicated(xdg, tmp_path):
    one, two = str(tmp_path / "lib1"), str(tmp_path / "lib2")
    _make(one, "A5001", b"first")
    _make(two, "A5001", b"second")
    author_image.migrate_into_shared_store([one, two])
    dest = os.path.join(author_image.images_dir(), "A5001.png")
    assert open(dest, "rb").read() == b"first", \
        "first one wins; the second is a duplicate of the same person"


def test_migration_is_safe_to_run_twice(xdg, tmp_path):
    one = str(tmp_path / "lib1")
    _make(one, "A5001")
    assert author_image.migrate_into_shared_store([one]) == 1
    assert author_image.migrate_into_shared_store([one]) == 0


def test_a_library_with_no_images_is_not_an_error(xdg, tmp_path):
    assert author_image.migrate_into_shared_store(
        [str(tmp_path / "empty")]) == 0


def test_a_missing_library_root_is_skipped(xdg):
    assert author_image.migrate_into_shared_store(
        ["/no/such/place"]) == 0


def test_nothing_is_left_behind_in_the_library(xdg, tmp_path):
    one = str(tmp_path / "lib1")
    p = _make(one, "A5001")
    author_image.migrate_into_shared_store([one])
    assert not os.path.exists(p), "moved, not copied"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
