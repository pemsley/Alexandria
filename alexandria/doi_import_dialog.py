"""Modal 'Import from DOI' dialog.

Resolves a pasted DOI to metadata (OpenAlex, CrossRef fallback) on a
worker thread, then hands a BibTeX-shaped record to the browser window's
add_reference_from_viewer on the GTK main thread (which owns the GUI DB
connection). Optionally chases the open-access PDF."""

import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from . import metrics, bibtex_import


def _clean_doi(s):
    """Reduce a pasted DOI string to a bare DOI: strip surrounding
    whitespace, a 'doi:' label, and doi.org / dx.doi.org URL prefixes."""
    s = (s or "").strip()
    if not s:
        return ""
    low = s.lower()
    for p in ("https://doi.org/", "http://doi.org/",
              "https://dx.doi.org/", "http://dx.doi.org/",
              "doi.org/", "dx.doi.org/", "doi:"):
        if low.startswith(p):
            s = s[len(p):]
            break
    return s.strip()


def open_doi_import(parent):
    """Open the modal. `parent` (the BrowserWindow) must expose
    add_reference_from_viewer(br, also_get_pdf, on_done) and
    _toast(message)."""
    win = Gtk.Window(transient_for=parent, modal=True)
    win.set_title("Import from DOI")
    win.set_default_size(440, -1)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    outer.set_margin_start(12)
    outer.set_margin_end(12)
    outer.set_margin_top(12)
    outer.set_margin_bottom(12)

    prompt = Gtk.Label(label="Enter a DOI to add to your library:")
    prompt.set_halign(Gtk.Align.START)
    outer.append(prompt)

    entry = Gtk.Entry()
    entry.set_placeholder_text("10.1107/S2059798320000534")
    entry.set_hexpand(True)
    outer.append(entry)

    pdf_check = Gtk.CheckButton(label="Also fetch the PDF")
    pdf_check.set_active(True)
    outer.append(pdf_check)

    status = Gtk.Label()
    status.set_halign(Gtk.Align.START)
    status.set_wrap(True)
    status.set_visible(False)
    outer.append(status)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_row.set_halign(Gtk.Align.END)
    cancel_btn = Gtk.Button(label="Cancel")
    add_btn = Gtk.Button(label="Add")
    add_btn.add_css_class("suggested-action")
    btn_row.append(cancel_btn)
    btn_row.append(add_btn)
    outer.append(btn_row)

    win.set_child(outer)

    def _set_busy(busy, msg=None):
        entry.set_sensitive(not busy)
        pdf_check.set_sensitive(not busy)
        add_btn.set_sensitive(not busy)
        if msg is not None:
            status.set_text(msg)
            status.set_visible(True)

    def _on_resolved(resolved, err, doi, also_pdf):
        if err:
            _set_busy(False, "Lookup failed: {}".format(err))
            return False
        if not resolved:
            _set_busy(False, "Couldn't find metadata for that DOI.")
            return False
        if not resolved.get("doi"):
            resolved["doi"] = doi
        br = bibtex_import.br_from_metadata(resolved)
        status.set_text("Adding…")

        def _on_done(success, message, label=None):
            if success:
                try:
                    parent._toast(message)
                except Exception:
                    pass
                win.close()
            else:
                _set_busy(False, message)
            return False

        parent.add_reference_from_viewer(br, also_pdf, _on_done)
        return False

    def _do_add(*_a):
        doi = _clean_doi(entry.get_text())
        if not doi:
            _set_busy(False, "Please enter a DOI.")
            return
        also_pdf = pdf_check.get_active()
        _set_busy(True, "Looking up metadata…")

        def _worker():
            try:
                resolved = metrics.resolve_doi(doi)
            except Exception as e:
                GLib.idle_add(_on_resolved, None, str(e), doi, also_pdf)
                return
            GLib.idle_add(_on_resolved, resolved, None, doi, also_pdf)

        threading.Thread(target=_worker, daemon=True).start()

    cancel_btn.connect("clicked", lambda _b: win.close())
    add_btn.connect("clicked", _do_add)
    entry.connect("activate", _do_add)

    win.present()
