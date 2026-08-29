"""The Authors window opened from the hamburger menu — no authorship
in hand: open_trail_window() shows the persisted trail, the empty
trail gets a helpful empty state, and the singleton is reused.

These construct real GTK widgets (no main loop), so they skip when
no display is available.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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

from alexandria import index, author_works


@pytest.fixture
def conn(tmp_path):
    c = index.open_db(str(tmp_path / "lib.db"))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fresh_singleton():
    author_works._authors_window = None
    yield
    author_works._authors_window = None


def test_empty_trail_shows_empty_state(conn):
    win = author_works.AuthorsWindow(conn)
    assert "No authors yet" in win._empty_lbl.get_label()


def test_empty_state_discover_button_fires_callback(conn):
    fired = []
    win = author_works.AuthorsWindow(
        conn, on_discover=lambda: fired.append(True))
    assert win._empty_discover_btn.get_visible()
    win._empty_discover_btn.emit("clicked")
    assert fired == [True]


def test_empty_state_hides_discover_button_without_callback(conn):
    win = author_works.AuthorsWindow(conn)
    assert not win._empty_discover_btn.get_visible()


def test_nonempty_trail_shows_select_prompt(conn):
    index.add_author_trail(
        conn, {"name": "A. Uthor", "orcid": "0000-0001-2345-6789"})
    win = author_works.AuthorsWindow(conn)
    assert "Select an author" in win._empty_lbl.get_label()
    assert not win._empty_discover_btn.get_visible()


def test_removing_last_author_restores_empty_state(conn):
    entry = index.add_author_trail(
        conn, {"name": "A. Uthor", "orcid": "0000-0001-2345-6789"})
    win = author_works.AuthorsWindow(conn)
    win._remove_author(entry["key"])
    assert "No authors yet" in win._empty_lbl.get_label()


def test_open_trail_window_needs_no_authorship_and_is_singleton(conn):
    w1 = author_works.open_trail_window(None, conn)
    w2 = author_works.open_trail_window(None, conn)
    assert w1 is w2
    assert isinstance(w1, author_works.AuthorsWindow)


def test_open_window_reuses_trail_window_singleton(conn):
    w1 = author_works.open_trail_window(None, conn)
    w2 = author_works.open_window(
        None, conn,
        {"name": "A. Uthor", "orcid": "0000-0001-2345-6789"})
    assert w2 is w1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
