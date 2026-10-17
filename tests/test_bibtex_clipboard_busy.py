"""The clipboard import claims `_import_busy` before it reads.

The other import routes open a modal file chooser, which stops a
second activation by itself. The clipboard route shows no dialog, so
the window stays live across the asynchronous read: if the flag were
only set in the callback, activating the menu item twice in quick
succession would start two imports on the shared connection.

Claiming it early means every path that then declines to import has
to hand it back, or the next import is refused for the rest of the
session. That is what these tests are really watching.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_display_ok = True
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib
    if not Gtk.init_check():
        _display_ok = False
    else:
        Adw.init()
except Exception:
    _display_ok = False

pytestmark = pytest.mark.skipif(
    not _display_ok, reason="no display for GTK tests")

from alexandria import browse


class _Clipboard:
    """Stands in for Gdk.Clipboard: records the async request and
    lets the test fire the callback when it chooses."""

    def __init__(self):
        self.callback = None

    def read_text_async(self, cancellable, callback):
        self.callback = callback


@pytest.fixture
def window():
    win = browse.BrowserWindow.__new__(browse.BrowserWindow)
    win._import_busy = False
    win._clipboard = _Clipboard()
    win._toasts = []
    win._status = []
    win.get_clipboard = lambda: win._clipboard
    win._toast = lambda msg, timeout=None: win._toasts.append(msg)
    win.status = type("S", (), {
        "set_text": lambda _s, t: win._status.append(t)})()
    return win


def test_the_flag_is_claimed_before_the_read_returns(window):
    """The window must already be busy while the clipboard read is
    still outstanding — that is the whole point."""
    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)

    assert window._clipboard.callback is not None, "no read was started"
    assert window._import_busy is True


def test_a_second_activation_is_refused_mid_read(window):
    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)
    first = window._clipboard.callback
    window._clipboard.callback = None

    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)

    assert window._clipboard.callback is None, "a second read was started"
    assert window._status == ["Import already running"]
    assert first is not None


def test_a_clipboard_without_bibtex_hands_the_claim_back(window):
    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)
    clip = type("C", (), {
        "read_text_finish": lambda _s, _r: "just some prose"})()

    browse.BrowserWindow._on_clipboard_bibtex_read(window, clip, None)

    assert window._import_busy is False, "importing is wedged"
    assert window._toasts == ["No BibTeX on the clipboard"]


def test_an_empty_clipboard_hands_the_claim_back(window):
    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)
    clip = type("C", (), {"read_text_finish": lambda _s, _r: None})()

    browse.BrowserWindow._on_clipboard_bibtex_read(window, clip, None)

    assert window._import_busy is False
    assert window._toasts == ["Nothing on the clipboard to import"]


def test_a_failed_read_hands_the_claim_back(window):
    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)

    def _boom(_s, _r):
        raise GLib.Error("clipboard went away")
    clip = type("C", (), {"read_text_finish": _boom})()

    browse.BrowserWindow._on_clipboard_bibtex_read(window, clip, None)

    assert window._import_busy is False
    assert len(window._toasts) == 1
    assert window._toasts[0].startswith("Couldn't read the clipboard")


def test_bibtex_on_the_clipboard_starts_the_import(window):
    """The success path leaves the flag alone — `_start_import_bib_text`
    sets it itself — so it must still be claimed afterwards."""
    started = []
    window._start_import_bib_text = started.append
    browse.BrowserWindow._on_import_bibtex_clipboard(window, None)
    clip = type("C", (), {
        "read_text_finish":
            lambda _s, _r: "@article{k, title={x}, year={2024}}"})()

    browse.BrowserWindow._on_clipboard_bibtex_read(window, clip, None)

    assert started == ["@article{k, title={x}, year={2024}}"]
    assert window._import_busy is True
    assert window._toasts == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
