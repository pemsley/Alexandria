"""Author photos in the main-window author popover.

The avatar is a curation signal, not decoration: a photo exists only
because someone went and found one, so in a fifty-name author list a
face is a scanning aid. That is why most rows must show *nothing* —
an initials disc for every author would destroy the signal by making
every row equal.

Until now avatars appeared only in the Authors window, so the effort
of setting one was invisible from the window where the time is spent.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw
    _display_ok = bool(Gtk.init_check())
    if _display_ok:
        Adw.init()
except Exception:
    _display_ok = False

pytestmark = pytest.mark.skipif(
    not _display_ok, reason="no display for GTK tests")

from alexandria import author_image, browse


@pytest.fixture
def window():
    """The uninitialised shell — `_author_avatar` needs no state
    beyond the class-level texture cache."""
    win = browse.BrowserWindow.__new__(browse.BrowserWindow)
    browse.BrowserWindow._avatar_textures.clear()
    yield win
    browse.BrowserWindow._avatar_textures.clear()


def _png(path):
    from gi.repository import GdkPixbuf
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 4, 4)
    pb.fill(0x3584e4ff)
    pb.savev(path, "png", [], [])


def test_an_author_with_a_photo_gets_one(window, tmp_path):
    a = {"name": "M. Steinegger", "openalex_id": "A1"}
    os.makedirs(author_image.images_dir(), exist_ok=True)
    _png(author_image.image_path(a))

    cell = window._author_avatar(a)

    assert isinstance(cell.get_first_child(), Adw.Avatar)


def test_an_author_without_one_gets_blank_space(window):
    """Not an initials disc: if every row has a mark, no row stands
    out, and standing out is the entire job."""
    cell = window._author_avatar({"name": "Nobody", "openalex_id": "A404"})

    assert cell.get_first_child() is None


def test_the_column_keeps_its_width_so_names_stay_aligned(window):
    empty = window._author_avatar({"name": "Nobody", "openalex_id": "A404"})
    width, _h = empty.get_size_request()
    assert width == browse.BrowserWindow._AVATAR_PX


def test_a_scraped_name_can_never_have_a_photo(window):
    """No OpenAlex ID and no ORCID means nowhere stable to keep one —
    the same gate the trail uses."""
    cell = window._author_avatar({"name": "Only A Name", "scraped": True})

    assert cell.get_first_child() is None


def test_the_texture_is_decoded_once_per_author(window, tmp_path):
    """The popover is rebuilt on every open and the same authors
    recur across cards; a 512px PNG drawn at 22px should not be
    decoded each time."""
    a = {"name": "M. Steinegger", "openalex_id": "A1"}
    os.makedirs(author_image.images_dir(), exist_ok=True)
    _png(author_image.image_path(a))

    window._author_avatar(a)
    cached = dict(browse.BrowserWindow._avatar_textures)
    os.remove(author_image.image_path(a))     # gone from disk
    cell = window._author_avatar(a)           # still drawn, from cache

    assert list(cached) == ["A1"]
    assert isinstance(cell.get_first_child(), Adw.Avatar)


def test_an_unreadable_file_degrades_to_blank(window):
    a = {"name": "Corrupt", "openalex_id": "A2"}
    os.makedirs(author_image.images_dir(), exist_ok=True)
    with open(author_image.image_path(a), "wb") as fh:
        fh.write(b"this is not a PNG")

    cell = window._author_avatar(a)

    assert cell.get_first_child() is None
