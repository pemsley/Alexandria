"""The Fetch button beside the DOI entry, wired end to end.

Constructs the real editor, clicks the real button, and checks the
fields fill — the lookup itself stubbed. Skips when there is no
display.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib
    _display_ok = bool(Gtk.init_check())
    if _display_ok:
        Adw.init()
except Exception:
    _display_ok = False

pytestmark = pytest.mark.skipif(
    not _display_ok, reason="no display for GTK tests")

from alexandria import edit_dialog, index, metrics, sidecar

DOI = "10.1006/jmbi.1995.0037"

FOUND = {
    "doi": DOI,
    "title": "Molecular recognition of receptor sites",
    "year": 1995,
    "journal": "Journal of Molecular Biology",
    "authors": ["Gareth Jones", "Peter Willett", "Robert C. Glen"],
    "volume": "245", "issue": "1", "pages": "43-53",
}


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        for g in _descendants(child):
            yield g
        child = child.get_next_sibling()


def _find_button(win, label):
    for w in _descendants(win):
        if isinstance(w, Gtk.Button) and w.get_label() == label:
            return w
    return None


def _doi_entry(win):
    """The entry sharing a box with the Fetch button — precise, and
    independent of how many other entries the dialog grows."""
    box = _find_button(win, "Fetch").get_parent()
    for w in _descendants(box):
        if isinstance(w, Gtk.Entry):
            return w
    return None


def _entry_texts(win):
    return [w.get_text() for w in _descendants(win)
            if isinstance(w, Gtk.Entry)]


def _pump(seconds=2.0, until=None):
    ctx = GLib.MainContext.default()
    t0 = time.time()
    while time.time() - t0 < seconds:
        while ctx.pending():
            ctx.iteration(False)
        if until is not None and until():
            return True
    return until() if until is not None else False


@pytest.fixture
def editor(tmp_path, monkeypatch):
    pdf = str(tmp_path / "molecular-recognition.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    sc = sidecar.sidecar_path_for(pdf)
    rec = sidecar.new_record(pdf)
    rec.update({"title": "", "authors": [], "doi": ""})
    sidecar.write(sc, rec)
    conn = index.open_db(str(tmp_path / "lib.db"))
    index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))

    before = set(Gtk.Window.get_toplevels())
    edit_dialog.open_editor(None, conn, pdf, sc, None)
    win = [w for w in Gtk.Window.get_toplevels() if w not in before][-1]
    yield win, sc
    win.destroy()


def test_the_button_is_there(editor):
    win, _sc = editor
    assert _find_button(win, "Fetch") is not None


def test_fetch_fills_the_fields_from_the_doi(editor, monkeypatch):
    win, _sc = editor
    monkeypatch.setattr(metrics, "fetch_work_by_doi", lambda d: FOUND)

    _doi_entry(win).set_text("https://doi.org/" + DOI)
    _find_button(win, "Fetch").emit("clicked")

    filled = _pump(until=lambda: FOUND["title"] in _entry_texts(win))
    assert filled, "title was not filled in"
    texts = _entry_texts(win)
    assert FOUND["journal"] in texts
    assert "1995" in texts
    assert "245" in texts and "43-53" in texts
    # the pasted URL is replaced by the bare DOI that will be saved
    assert _doi_entry(win).get_text() == DOI


def test_unknown_doi_says_so_and_changes_nothing(editor, monkeypatch):
    win, _sc = editor
    monkeypatch.setattr(metrics, "fetch_work_by_doi", lambda d: None)
    _doi_entry(win).set_text(DOI)

    btn = _find_button(win, "Fetch")
    btn.emit("clicked")
    _pump(1.0)

    assert FOUND["title"] not in _entry_texts(win)
    assert btn.get_sensitive()        # re-enabled, not left stuck


def test_junk_in_the_doi_field_never_reaches_openalex(editor, monkeypatch):
    win, _sc = editor
    asked = []
    monkeypatch.setattr(metrics, "fetch_work_by_doi",
                        lambda d: asked.append(d) or FOUND)
    _doi_entry(win).set_text("no idea, sorry")

    _find_button(win, "Fetch").emit("clicked")
    _pump(0.5)

    assert asked == []
