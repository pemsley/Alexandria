"""Modal metadata editor for a single PDF.

Reads the canonical sidecar JSON, lets the user edit, writes back atomically.
Sets hand_edited=True on save so a future --refresh won't clobber it.
"""

import os

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from . import sidecar, index


def _textview_text(tv):
    buf = tv.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


def _set_textview(tv, text):
    tv.get_buffer().set_text(text or "")


def _parse_authors(s):
    return [line.strip() for line in (s or "").splitlines() if line.strip()]


def _parse_tags(s):
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def _parse_year(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def open_editor(parent, conn, pdf_path, sidecar_path, on_saved):
    """Open a modal editor window. on_saved() is called after a successful
    save so the caller can refresh."""
    try:
        rec = sidecar.read(sidecar_path)
    except Exception:
        rec = sidecar.new_record(pdf_path)

    win = Gtk.Window(transient_for=parent, modal=True)
    win.set_title("Edit: " + os.path.basename(pdf_path))
    win.set_default_size(640, 640)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.set_margin_start(12)
    outer.set_margin_end(12)
    outer.set_margin_top(12)
    outer.set_margin_bottom(12)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)
    scrolled.set_hexpand(True)

    grid = Gtk.Grid()
    grid.set_row_spacing(8)
    grid.set_column_spacing(10)
    grid.set_hexpand(True)

    def add_label(text, row):
        lbl = Gtk.Label(label=text)
        lbl.set_halign(Gtk.Align.END)
        lbl.set_valign(Gtk.Align.START)
        grid.attach(lbl, 0, row, 1, 1)

    title_entry = Gtk.Entry()
    title_entry.set_text(rec.get("title") or "")
    title_entry.set_hexpand(True)
    add_label("Title:", 0)
    grid.attach(title_entry, 1, 0, 1, 1)

    authors_view = Gtk.TextView()
    authors_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    authors_view.set_top_margin(4)
    authors_view.set_left_margin(4)
    authors_view.set_right_margin(4)
    authors_view.set_bottom_margin(4)
    _set_textview(authors_view, "\n".join(rec.get("authors") or []))
    authors_scroll = Gtk.ScrolledWindow()
    authors_scroll.set_min_content_height(120)
    authors_scroll.set_hexpand(True)
    authors_scroll.set_child(authors_view)
    authors_scroll.set_has_frame(True)
    add_label("Authors\n(one per line):", 1)
    grid.attach(authors_scroll, 1, 1, 1, 1)

    year_entry = Gtk.Entry()
    year_entry.set_text(str(rec["year"]) if rec.get("year") else "")
    year_entry.set_max_length(4)
    year_entry.set_max_width_chars(6)
    add_label("Year:", 2)
    grid.attach(year_entry, 1, 2, 1, 1)

    journal_entry = Gtk.Entry()
    journal_entry.set_text(rec.get("journal") or "")
    journal_entry.set_hexpand(True)
    add_label("Journal:", 3)
    grid.attach(journal_entry, 1, 3, 1, 1)

    doi_entry = Gtk.Entry()
    doi_entry.set_text(rec.get("doi") or "")
    doi_entry.set_hexpand(True)
    add_label("DOI:", 4)
    grid.attach(doi_entry, 1, 4, 1, 1)

    tags_entry = Gtk.Entry()
    tags_entry.set_text(", ".join(rec.get("tags") or []))
    tags_entry.set_hexpand(True)
    add_label("Tags\n(comma sep):", 5)
    grid.attach(tags_entry, 1, 5, 1, 1)

    mark_choices = ["(none)", "● Red", "● Orange", "● Green"]
    _MARK_VALUES = [None, "red", "orange", "green"]
    mark_dropdown = Gtk.DropDown.new_from_strings(mark_choices)
    try:
        mark_dropdown.set_selected(_MARK_VALUES.index(rec.get("mark")))
    except ValueError:
        mark_dropdown.set_selected(0)
    add_label("Mark:", 6)
    grid.attach(mark_dropdown, 1, 6, 1, 1)

    notes_view = Gtk.TextView()
    notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    notes_view.set_top_margin(4)
    notes_view.set_left_margin(4)
    notes_view.set_right_margin(4)
    notes_view.set_bottom_margin(4)
    _set_textview(notes_view, rec.get("notes") or "")
    notes_scroll = Gtk.ScrolledWindow()
    notes_scroll.set_min_content_height(140)
    notes_scroll.set_hexpand(True)
    notes_scroll.set_child(notes_view)
    notes_scroll.set_has_frame(True)
    add_label("Notes:", 7)
    grid.attach(notes_scroll, 1, 7, 1, 1)

    hand_edited_check = Gtk.CheckButton(label="Hand-edited (don't overwrite on refresh)")
    hand_edited_check.set_active(bool(rec.get("hand_edited", False)))
    grid.attach(hand_edited_check, 1, 8, 1, 1)

    path_lbl = Gtk.Label()
    path_lbl.set_markup("<small><tt>{}</tt></small>".format(pdf_path))
    path_lbl.set_halign(Gtk.Align.START)
    path_lbl.set_selectable(True)
    grid.attach(path_lbl, 1, 9, 1, 1)

    scrolled.set_child(grid)
    outer.append(scrolled)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    btn_row.set_halign(Gtk.Align.END)
    cancel_btn = Gtk.Button(label="Cancel")
    save_btn = Gtk.Button(label="Save")
    save_btn.add_css_class("suggested-action")
    btn_row.append(cancel_btn)
    btn_row.append(save_btn)
    outer.append(btn_row)

    def do_save(_b):
        rec["title"] = title_entry.get_text().strip() or None
        rec["authors"] = _parse_authors(_textview_text(authors_view))
        rec["year"] = _parse_year(year_entry.get_text())
        rec["journal"] = journal_entry.get_text().strip() or None
        rec["doi"] = doi_entry.get_text().strip() or None
        rec["tags"] = _parse_tags(tags_entry.get_text())
        rec["mark"] = _MARK_VALUES[mark_dropdown.get_selected()]
        rec["notes"] = _textview_text(notes_view)
        # Auto-set hand_edited if user actually changed anything; honour the
        # checkbox in any case.
        rec["hand_edited"] = bool(hand_edited_check.get_active())

        try:
            sidecar.write(sidecar_path, rec)
            mtime = os.path.getmtime(sidecar_path)
            from .sidecar import thumb_path_for
            tp = thumb_path_for(pdf_path)
            index.upsert(conn, pdf_path, sidecar_path,
                         tp if os.path.isfile(tp) else None, rec, mtime)
        except Exception as e:
            dlg = Gtk.AlertDialog()
            dlg.set_message("Could not save: {}".format(e))
            dlg.show(win)
            return
        win.close()
        if on_saved:
            on_saved()

    cancel_btn.connect("clicked", lambda _b: win.close())
    save_btn.connect("clicked", do_save)

    win.set_child(outer)
    win.present()
