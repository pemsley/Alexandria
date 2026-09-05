"""Modal metadata editor for a single PDF.

Reads the canonical sidecar JSON, lets the user edit, writes back atomically.
Sets hand_edited=True on save so a future --refresh won't clobber it.
"""

import os
import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from . import sidecar, index, marks_config, metrics, bibtex_export


def _mark_dropdown(initial_idx):
    """Mark dropdown for the editor: (none) / Red / Orange / Green
    with coloured bullets. Standalone copy of the helper in browse.py
    to avoid pulling browse.py (and Poppler etc.) into the editor."""
    labels = marks_config.load()
    items = [("(none)", None)]
    for c, color in (("red",    "#cc3333"),
                     ("orange", "#ee8800"),
                     ("green",  "#33aa33"),
                     ("cyan",   "#33aaaa")):
        items.append((
            marks_config.display_for(c, c.capitalize(), labels),
            color,
        ))
    sl = Gtk.StringList()
    for label, _ in items:
        sl.append(label)
    factory = Gtk.SignalListItemFactory()

    def _setup(_f, li):
        li.set_child(Gtk.Label(xalign=0.0))

    def _bind(_f, li):
        lbl = li.get_child()
        label, color = items[li.get_position()]
        if color:
            lbl.set_markup(
                '<span foreground="{}"><b>●</b></span>   {}'.format(
                    color, GLib.markup_escape_text(label)))
        else:
            lbl.set_markup(GLib.markup_escape_text(label))

    factory.connect("setup", _setup)
    factory.connect("bind", _bind)
    dd = Gtk.DropDown(model=sl, factory=factory)
    dd.set_selected(initial_idx)
    return dd


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
    win.set_default_size(640, 720)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.set_margin_start(12)
    outer.set_margin_end(12)
    outer.set_margin_top(12)
    outer.set_margin_bottom(12)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)
    scrolled.set_hexpand(True)
    # Never scroll the form sideways: with AUTOMATIC horizontal
    # policy, focusing an entry scrolls it into view and slides the
    # label column off-screen.
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

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

    # Citation key. A human artefact — people have typed
    # `emsley2010features` into LaTeX for decades, and a paper's key
    # often has to match one already in a group's .bib or a
    # collaborator's manuscript. Left empty, export generates the
    # same suggestion Suggest offers, so filling this in is only
    # needed when you want a particular key.
    key_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    key_entry = Gtk.Entry()
    key_entry.set_text(rec.get("bibtex_key") or "")
    key_entry.set_hexpand(True)
    key_entry.set_placeholder_text("e.g. emsley2010features")
    key_box.append(key_entry)
    suggest_btn = Gtk.Button(label="Suggest")
    suggest_btn.set_tooltip_text(
        "Build a key from the author, year and title above")
    key_box.append(suggest_btn)

    def _on_suggest(_b):
        # Built from what is in the dialog now, not from the saved
        # record: the point is usually to key a paper whose metadata
        # you have just corrected.
        key_entry.set_text(bibtex_export.suggest_key({
            "authors": _parse_authors(_textview_text(authors_view)),
            "year": _parse_year(year_entry.get_text()),
            "title": title_entry.get_text().strip(),
        }))

    suggest_btn.connect("clicked", _on_suggest)
    add_label("Citation key:", 5)
    grid.attach(key_box, 1, 5, 1, 1)

    # --- Find metadata: citation disambiguation ------------------
    # The user can see "Jones et al., JMB, 1995" on the paper even
    # when extraction got nothing. Parse the fragment, query
    # OpenAlex for ranked candidates (first-author match first,
    # then citations), and let a click fill the fields for review
    # before Save. Deliberately NOT a grid row: this is a tool that
    # writes the fields, not a field itself — it lives in its own
    # frame at the top of the dialog.
    find_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    find_box.set_margin_start(8)
    find_box.set_margin_end(8)
    find_box.set_margin_top(6)
    find_box.set_margin_bottom(8)
    find_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    find_entry = Gtk.Entry()
    find_entry.set_placeholder_text("e.g. Jones et al., JMB, 1995")
    find_entry.set_hexpand(True)
    find_btn = Gtk.Button(label="Search")
    find_row.append(find_entry)
    find_row.append(find_btn)
    find_box.append(find_row)
    find_status = Gtk.Label(xalign=0.0)
    find_status.set_wrap(True)
    # Cap the natural width — a long wrapping label otherwise
    # widens the whole dialog to its unwrapped length.
    find_status.set_max_width_chars(54)
    find_status.set_visible(False)
    find_box.append(find_status)
    results_list = Gtk.ListBox()
    results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
    results_scroll = Gtk.ScrolledWindow()
    results_scroll.set_min_content_height(160)
    results_scroll.set_policy(Gtk.PolicyType.NEVER,
                              Gtk.PolicyType.AUTOMATIC)
    results_scroll.set_has_frame(True)
    results_scroll.set_visible(False)
    results_scroll.set_child(results_list)
    find_box.append(results_scroll)
    find_frame = Gtk.Frame()
    find_frame.set_label("Find metadata — fills the fields below")
    find_frame.set_child(find_box)

    def _find_status(text):
        find_status.set_markup(
            "<span size='small' alpha='75%'>{}</span>".format(
                GLib.markup_escape_text(text)))
        find_status.set_visible(True)

    def apply_candidate(c):
        title_entry.set_text(c.get("title") or "")
        _set_textview(authors_view, "\n".join(c.get("authors") or []))
        year_entry.set_text(str(c["year"]) if c.get("year") else "")
        journal_entry.set_text(c.get("journal") or "")
        doi_entry.set_text(c.get("doi") or "")
        _find_status("Filled from OpenAlex — review, then Save.")

    def show_results(mode, cands):
        find_btn.set_sensitive(True)
        child = results_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            results_list.remove(child)
            child = nxt
        if not cands:
            _find_status("No matches on OpenAlex.")
            results_scroll.set_visible(False)
            return False
        note = {"exact": "", "minus1": " (year matched ±1)",
                "none": " (year ignored)"}.get(mode, "")
        msg = "{} candidate{}{}".format(
            len(cands), "" if len(cands) == 1 else "s", note)
        if (len(cands) >= 2 and cands[1]["cited_by_count"]
                and cands[0]["cited_by_count"]):
            msg += " — top result {:.0f}× more cited than next".format(
                cands[0]["cited_by_count"] / cands[1]["cited_by_count"])
        _find_status(msg)
        for c in cands:
            row = Gtk.ListBoxRow()
            row._cand = c
            lbl = Gtk.Label(xalign=0.0)
            lbl.set_wrap(True)
            lbl.set_max_width_chars(58)
            auths = c.get("authors") or []
            auth_line = ", ".join(auths[:3]) + (
                ", et al." if len(auths) > 3 else "")
            lbl.set_markup(
                "<b>{}×</b>  {}\n<span size='small' alpha='75%'>"
                "{} · {} · {} · {}</span>".format(
                    c["cited_by_count"],
                    GLib.markup_escape_text(c.get("title") or "(no title)"),
                    GLib.markup_escape_text(
                        auth_line or c.get("first_author") or "?"),
                    GLib.markup_escape_text(c.get("journal") or "?"),
                    c.get("year") or "?",
                    GLib.markup_escape_text(c.get("doi") or "no DOI")))
            lbl.set_margin_top(3)
            lbl.set_margin_bottom(3)
            lbl.set_margin_start(6)
            lbl.set_margin_end(6)
            row.set_child(lbl)
            results_list.append(row)
        results_scroll.set_visible(True)
        return False

    def on_find(_w):
        hint = metrics.parse_citation_hint(find_entry.get_text())
        if not hint["surname"]:
            _find_status("Couldn't read an author surname — try "
                         "something like \"Jones et al., JMB, 1995\".")
            return
        _find_status("Searching OpenAlex: {} / {} / {}…".format(
            hint["surname"], hint["journal"] or "any journal",
            hint["year"] or "any year"))
        find_btn.set_sensitive(False)

        def work():
            try:
                mode, cands = metrics.find_citation_candidates(
                    hint["surname"], hint["year"], hint["journal"])
            except Exception as e:
                GLib.idle_add(_find_status,
                              "Search failed: {}".format(e))
                GLib.idle_add(find_btn.set_sensitive, True)
                return
            GLib.idle_add(show_results, mode, cands)
        threading.Thread(target=work, daemon=True).start()

    find_btn.connect("clicked", on_find)
    find_entry.connect("activate", on_find)
    results_list.connect(
        "row-activated",
        lambda _lb, row: apply_candidate(row._cand))

    tags_entry = Gtk.Entry()
    tags_entry.set_text(", ".join(rec.get("tags") or []))
    tags_entry.set_hexpand(True)
    add_label("Tags\n(comma sep):", 6)
    grid.attach(tags_entry, 1, 6, 1, 1)

    _MARK_VALUES = [None, "red", "orange", "green", "cyan"]
    try:
        initial_idx = _MARK_VALUES.index(rec.get("mark"))
    except ValueError:
        initial_idx = 0
    mark_dropdown = _mark_dropdown(initial_idx)
    add_label("Mark:", 7)
    grid.attach(mark_dropdown, 1, 7, 1, 1)

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
    add_label("Notes:", 8)
    grid.attach(notes_scroll, 1, 8, 1, 1)

    hand_edited_check = Gtk.CheckButton(label="Hand-edited (don't overwrite on refresh)")
    hand_edited_check.set_active(bool(rec.get("hand_edited", False)))
    grid.attach(hand_edited_check, 1, 9, 1, 1)

    path_lbl = Gtk.Label()
    path_lbl.set_markup("<small><tt>{}</tt></small>".format(pdf_path))
    path_lbl.set_halign(Gtk.Align.START)
    path_lbl.set_selectable(True)
    grid.attach(path_lbl, 1, 10, 1, 1)

    scrolled.set_child(grid)
    outer.append(find_frame)
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
        rec["bibtex_key"] = (
            bibtex_export.sanitise_key(key_entry.get_text()) or None)
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
