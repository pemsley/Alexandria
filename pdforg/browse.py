#!/usr/bin/env python3
"""GTK4 browser for the PDF library.

Reads from the local SQLite index. Sidecars are the source of truth;
this window is read-only for v1 (click → xdg-open the PDF)."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GLib, Gio, Pango

from . import index, edit_dialog, importer, metrics, sidecar, extract

LIBRARY_ROOT = os.environ.get(
    "PDFORG_LIBRARY", os.path.expanduser("~/pdfs"))


def open_pdf(path):
    try:
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        print("open failed:", e)


_SAFE_INLINE_TAGS = ("i", "b", "u", "s", "em", "strong",
                     "sub", "sup", "small", "tt")
_SAFE_TAG_RE = re.compile(
    r"</?(?:" + "|".join(_SAFE_INLINE_TAGS) + r")\s*/?>", re.IGNORECASE)

# Pad missing spaces around inline tags: when a tag butts up against a
# word character, insert a single space. Handles both opening tags
# preceded by a word ("the<i>X") and closing tags followed by one
# ("X</i>foo"). Punctuation is left alone.
_PAD_OPEN_RE = re.compile(
    r"(\w)(<(?:" + "|".join(_SAFE_INLINE_TAGS) + r")\b[^>]*>)", re.IGNORECASE)
_PAD_CLOSE_RE = re.compile(
    r"(</(?:" + "|".join(_SAFE_INLINE_TAGS) + r")\s*>)(\w)", re.IGNORECASE)


def _pad_inline_tags(text):
    text = _PAD_OPEN_RE.sub(r"\1 \2", text)
    text = _PAD_CLOSE_RE.sub(r"\1 \2", text)
    return text


_PLACEHOLDER_OPEN = ""   # private-use Unicode, won't appear in real text
_PLACEHOLDER_CLOSE = ""
_PLACEHOLDER_RE = re.compile(_PLACEHOLDER_OPEN + r"(\d+)" + _PLACEHOLDER_CLOSE)


def safe_pango_markup(text):
    """Escape `text` for Pango markup, preserving a whitelist of inline
    formatting tags (<i>, <b>, <sub>, <sup>, ...). Everything else —
    stray '<', '>', '&', etc. — is escaped. Returns a string that's
    safe to pass to Gtk.Label.set_markup()."""
    if not text:
        return ""
    text = _pad_inline_tags(text)
    placeholders = []

    def _capture(m):
        placeholders.append(m.group(0))
        return "{}{}{}".format(
            _PLACEHOLDER_OPEN, len(placeholders) - 1, _PLACEHOLDER_CLOSE)

    protected = _SAFE_TAG_RE.sub(_capture, text)
    escaped = GLib.markup_escape_text(protected)

    def _restore(m):
        return placeholders[int(m.group(1))]

    return _PLACEHOLDER_RE.sub(_restore, escaped)


_PREPRINT_DOI_PREFIXES = (
    "10.1101/",       # bioRxiv / medRxiv
    "10.48550/",      # arXiv (assigned DOIs)
    "10.26434/",      # chemRxiv
    "10.21203/rs",    # Research Square
    "10.22541/au",    # Authorea
    "10.2139/ssrn",   # SSRN
    "10.31234/",      # PsyArXiv
    "10.31219/",      # OSF Preprints
    "10.20944/",      # Preprints.org
    "10.36227/",      # TechRxiv
)
_PREPRINT_JOURNAL_NEEDLES = (
    "biorxiv", "medrxiv", "arxiv", "chemrxiv", "research square",
    "authorea", "ssrn", "preprints.org", "techrxiv", "psyarxiv",
)


def is_preprint(row):
    doi = (row.get("doi") or "").lower()
    if any(doi.startswith(p) for p in _PREPRINT_DOI_PREFIXES):
        return True
    journal = (row.get("journal") or "").lower()
    return any(needle in journal for needle in _PREPRINT_JOURNAL_NEEDLES)


def make_keyword_chip(text):
    """A small framed chip used to display an auto-keyword (OpenAlex
    concept). Visually distinct from user-set tags (when those get UI)
    by being smaller and using muted text."""
    frame = Gtk.Frame()
    frame.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label()
    lbl.set_markup('<span foreground="#666666"><small>{}</small></span>'.format(
        GLib.markup_escape_text(text)))
    lbl.set_margin_start(6)
    lbl.set_margin_end(6)
    lbl.set_margin_top(1)
    lbl.set_margin_bottom(1)
    frame.set_child(lbl)
    return frame


_MARK_COLORS = {
    "red":    "#cc3333",
    "orange": "#ee8800",
    "green":  "#33aa33",
}


def make_mark_badge(mark):
    """A small framed coloured-circle chip for the user 'Mark' field.
    Returns None when no mark is set."""
    if not mark:
        return None
    color = _MARK_COLORS.get(mark)
    if not color:
        return None
    frame = Gtk.Frame()
    frame.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label()
    lbl.set_markup('<span foreground="{}"><b>●</b></span>'.format(color))
    lbl.set_margin_start(5)
    lbl.set_margin_end(5)
    lbl.set_margin_top(1)
    lbl.set_margin_bottom(1)
    lbl.set_tooltip_text("Mark: " + mark)
    frame.set_child(lbl)
    return frame


def make_preprint_badge():
    """A small 'PRE' chip to flag preprint entries."""
    frame = Gtk.Frame()
    frame.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label()
    lbl.set_markup('<span foreground="#cc6600" weight="bold"><small>PRE</small></span>')
    lbl.set_margin_start(5)
    lbl.set_margin_end(5)
    lbl.set_margin_top(1)
    lbl.set_margin_bottom(1)
    lbl.set_tooltip_text("Preprint")
    frame.set_child(lbl)
    return frame


def citation_stars_markup(n):
    """Pango markup for the citation-stars badge, or '' if below threshold."""
    if n is None:
        return ""
    if n >= 800:
        return ('<span foreground="#e89b00" weight="bold">'
                '★★★★★ Citation Classic Double</span>')
    if n >= 400:
        return ('<span foreground="#6bbe23" weight="bold">'
                '★★★★ Citation Classic</span>')
    if n >= 200:
        return '<span foreground="#888888">★★★</span>'
    if n >= 100:
        return '<span foreground="#888888">★★</span>'
    if n >= 50:
        return '<span foreground="#888888">★</span>'
    return ""


def authors_str(authors_json):
    try:
        a = json.loads(authors_json or "[]")
    except Exception:
        return ""
    if not a:
        return ""
    if len(a) > 4:
        return ", ".join(a[:4]) + " et al."
    return ", ".join(a)


def make_card(row, parent_window, conn, on_saved):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_start(8)
    box.set_margin_end(8)
    box.set_margin_top(6)
    box.set_margin_bottom(6)

    img = Gtk.Image()
    img.set_pixel_size(120)
    img.set_from_icon_name("application-pdf")
    if row["thumb_path"] and os.path.isfile(row["thumb_path"]):
        try:
            tex = Gdk.Texture.new_from_file(Gio.File.new_for_path(row["thumb_path"]))
            img.set_from_paintable(tex)
        except Exception:
            pass
    frame = Gtk.Frame()
    frame.set_size_request(130, 160)
    frame.set_child(img)
    frame.set_cursor_from_name("pointer")
    frame.set_tooltip_text("Open PDF")
    click = Gtk.GestureClick.new()
    click.set_button(1)
    click.connect("released", lambda *_: open_pdf(row["pdf_path"]))
    frame.add_controller(click)
    box.append(frame)

    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    text.set_hexpand(True)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    open_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
    open_btn.set_tooltip_text("Open PDF")
    open_btn.connect("clicked", lambda _b: open_pdf(row["pdf_path"]))
    btn_row.append(open_btn)
    edit_btn = Gtk.Button.new_from_icon_name("document-properties-symbolic")
    edit_btn.set_tooltip_text("Edit metadata")
    edit_btn.connect(
        "clicked",
        lambda _b: edit_dialog.open_editor(
            parent_window, conn,
            row["pdf_path"], row["sidecar_path"], on_saved))
    btn_row.append(edit_btn)
    rename_btn = Gtk.Button.new_from_icon_name("edit-rename-symbolic")
    rename_btn.set_tooltip_text("Rename PDF")
    rename_btn.connect("clicked",
                       lambda _b: parent_window._open_rename_dialog(row))
    btn_row.append(rename_btn)
    delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
    delete_btn.set_tooltip_text("Delete PDF from library")
    delete_btn.connect("clicked",
                       lambda _b: parent_window._confirm_delete(row))
    btn_row.append(delete_btn)
    path_lbl = Gtk.Label()
    path_lbl.set_markup("<small><tt>{}</tt></small>".format(
        GLib.markup_escape_text(row["pdf_path"])))
    path_lbl.set_halign(Gtk.Align.START)
    path_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    path_lbl.set_max_width_chars(70)
    path_lbl.set_selectable(True)
    btn_row.append(path_lbl)
    text.append(btn_row)

    title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    mark_badge = make_mark_badge(row["mark"])
    if mark_badge is not None:
        title_row.append(mark_badge)
    if is_preprint(row):
        title_row.append(make_preprint_badge())
    title = Gtk.Label()
    title.set_markup("<b>{}</b>".format(
        safe_pango_markup(row["title"] or "(untitled)")))
    title.set_halign(Gtk.Align.START)
    title.set_wrap(True)
    title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    title.set_max_width_chars(80)
    title.set_selectable(True)
    title.set_hexpand(True)
    title_row.append(title)
    text.append(title_row)

    auth_text = authors_str(row["authors_json"])
    if len(auth_text) > 120:
        auth_text = auth_text[:117] + "..."
    auth = Gtk.Label()
    auth.set_markup("<small>{}</small>".format(GLib.markup_escape_text(auth_text)))
    auth.set_halign(Gtk.Align.START)
    auth.set_ellipsize(Pango.EllipsizeMode.END)
    auth.set_max_width_chars(80)
    text.append(auth)

    yj_bits = []
    if row["year"]:
        yj_bits.append(str(row["year"]))
    if row["journal"]:
        yj_bits.append(row["journal"])
    if row["citations"] is not None:
        yj_bits.append("cited {}×".format(row["citations"]))
    if yj_bits:
        yj = Gtk.Label()
        yj.set_markup("<small><i>{}</i></small>".format(
            GLib.markup_escape_text("  ·  ".join(yj_bits))))
        yj.set_halign(Gtk.Align.START)
        text.append(yj)

    stars = citation_stars_markup(row["citations"])
    if stars:
        star_lbl = Gtk.Label()
        star_lbl.set_markup("<small>{}</small>".format(stars))
        star_lbl.set_halign(Gtk.Align.START)
        text.append(star_lbl)

    # Auto-keywords (OpenAlex concepts). Capped to 5 visible to avoid
    # card bloat; tooltip on each chip shows the full topic name.
    auto_kw_json = row["auto_keywords_json"] if "auto_keywords_json" in row.keys() else None
    try:
        auto_kw = json.loads(auto_kw_json or "[]")
    except (TypeError, ValueError):
        auto_kw = []
    if auto_kw:
        kw_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for kw in auto_kw[:5]:
            kw_row.append(make_keyword_chip(kw))
        text.append(kw_row)

    box.append(text)
    return box


class BrowserWindow(Gtk.ApplicationWindow):
    def __init__(self, app, conn):
        super().__init__(application=app)
        self.conn = conn
        self.set_title("PDF Organizer")
        self.set_default_size(900, 700)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_start(6)
        outer.set_margin_end(6)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        import_files_btn = Gtk.Button(label="Import Files…")
        import_files_btn.connect("clicked", self._on_import_files)
        toolbar.append(import_files_btn)
        import_dir_btn = Gtk.Button(label="Import Folder…")
        import_dir_btn.connect("clicked", self._on_import_folder)
        toolbar.append(import_dir_btn)
        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text("Search title / authors / DOI / journal")
        self.search.connect("search-changed", self._on_search)
        toolbar.append(self.search)
        self.status = Gtk.Label()
        self.status.set_halign(Gtk.Align.END)
        toolbar.append(self.status)
        outer.append(toolbar)

        # Progress strip (hidden when idle).
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.progress_label = Gtk.Label(xalign=0.0)
        self.progress_label.set_hexpand(True)
        self.progress_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_valign(Gtk.Align.CENTER)
        self.progress_box.append(self.progress_label)
        self.progress_box.append(self.progress_bar)
        self.progress_box.set_visible(False)
        outer.append(self.progress_box)
        self._import_busy = False

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled.set_child(self.results)
        outer.append(scrolled)

        self.set_child(outer)
        self._reload(None)

        # Drop target: accept files (Gdk.FileList) dragged in from the
        # file manager. Copy each PDF into LIBRARY_ROOT and import it;
        # duplicates are detected and the copy discarded.
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        # Ctrl-F focuses the search entry.
        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
            action=Gtk.CallbackAction.new(self._focus_search)))
        self.add_controller(shortcuts)

        # Background citation-count refresh.
        self._cit_stop = threading.Event()
        self._cit_failed_session = set()
        threading.Thread(target=self._citation_refresher,
                         daemon=True).start()

        # Warn if pdfx isn't available — metadata extraction is much
        # weaker without it.
        if not extract._have_pdfx():
            GLib.idle_add(self._warn_no_pdfx)

    def _warn_no_pdfx(self):
        dlg = Gtk.AlertDialog()
        dlg.set_modal(True)
        dlg.set_message("pdfx not found")
        dlg.set_detail(
            "The 'pdfx' tool was not found on $PATH and the "
            "PDFORG_PDFX environment variable is not set.\n\n"
            "Metadata extraction will be compromised — titles, authors, "
            "DOI and journal will be sourced only from the PDF's basic "
            "/Info dictionary (often empty), with CrossRef enrichment "
            "as a fallback.\n\n"
            "To fix: install pdfx (pip install pdfx), or set "
            "PDFORG_PDFX=/path/to/pdfx in your environment.")
        dlg.set_buttons(["OK"])
        dlg.set_default_button(0)
        dlg.show(self)
        return False

    def _focus_search(self, *_args):
        self.search.grab_focus()
        self.search.select_region(0, -1)
        return True

    def _on_search(self, entry):
        self._reload(entry.get_text() or None)

    # --- Import (file dialog + background thread) -----------------------

    def _on_import_files(self, _btn):
        if self._import_busy:
            self.status.set_text("Import already running")
            return
        dlg = Gtk.FileDialog()
        dlg.set_title("Import PDF files")
        f = Gtk.FileFilter()
        f.set_name("PDF files")
        f.add_pattern("*.pdf")
        f.add_pattern("*.PDF")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)
        dlg.set_default_filter(f)
        dlg.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dlg, result):
        try:
            files = dlg.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = [f.get_path() for f in files if f and f.get_path()]
        paths = [p for p in paths if p.lower().endswith(".pdf")]
        if not paths:
            self.status.set_text("No PDFs selected")
            return
        self._start_import_paths(paths)

    def _on_import_folder(self, _btn):
        if self._import_busy:
            self.status.set_text("Import already running")
            return
        dlg = Gtk.FileDialog()
        dlg.set_title("Import folder of PDFs")
        dlg.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dlg, result):
        try:
            folder = dlg.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None:
            return
        path = folder.get_path()
        if not path:
            return
        self._start_import_tree(path)

    def _start_import_paths(self, paths):
        self._show_progress("Importing {} file(s)...".format(len(paths)), 0.0)
        self._import_busy = True
        threading.Thread(target=self._do_import_paths,
                         args=(paths,), daemon=True).start()

    def _start_import_tree(self, root):
        self._show_progress("Scanning {}...".format(root), 0.0)
        self._import_busy = True
        threading.Thread(target=self._do_import_tree,
                         args=(root,), daemon=True).start()

    def _do_import_paths(self, paths):
        self._run_import(paths)

    def _do_import_tree(self, root):
        try:
            paths = list(importer.find_pdfs(root))
        except Exception as e:
            GLib.idle_add(self._end_progress, "Scan failed: {}".format(e))
            return
        if not paths:
            GLib.idle_add(self._end_progress, "No PDFs under " + root)
            return
        self._run_import(paths)

    def _run_import(self, paths):
        n = len(paths)
        for i, p in enumerate(paths, 1):
            try:
                rec, status = importer.import_pdf(self.conn, p)
            except Exception as e:
                print("import failed for {}: {}".format(p, e))
                rec, status = None, "error"
            GLib.idle_add(self._update_progress, i, n, p, rec, status)
        GLib.idle_add(self._end_progress, None)

    def _show_progress(self, text, fraction):
        self.progress_label.set_text(text)
        self.progress_bar.set_fraction(fraction)
        self.progress_box.set_visible(True)

    def _update_progress(self, i, n, path, rec, status):
        bits = []
        if status == "duplicate":
            bits.append("DUP")
        elif status == "error":
            bits.append("ERR")
        elif status == "existing":
            bits.append("=")
        bits.append("[{}/{}]".format(i, n))
        bits.append(os.path.basename(path))
        if rec and status != "duplicate":
            a = rec.get("authors") or []
            if a:
                if len(a) > 2:
                    bits.append("- " + ", ".join(a[:2]) + " et al.")
                else:
                    bits.append("- " + ", ".join(a))
            if rec.get("year"):
                bits.append("({})".format(rec["year"]))
        elif status == "duplicate" and rec:
            bits.append("of " + os.path.basename(rec.get("pdf_path") or ""))
        self.progress_label.set_text(" ".join(bits))
        self.progress_bar.set_fraction(i / n if n else 1.0)
        return False

    def _end_progress(self, msg):
        self._import_busy = False
        self.progress_box.set_visible(False)
        self._reload(self.search.get_text() or None)
        if msg:
            self.status.set_text(msg)
        return False

    # --- Delete / Rename ------------------------------------------------

    def _confirm_delete(self, row):
        dlg = Gtk.AlertDialog()
        dlg.set_modal(True)
        dlg.set_message("Delete this PDF from the library?")
        dlg.set_detail("This will remove:\n  {}\n  + sidecar + thumbnail".format(
            row["pdf_path"]))
        dlg.set_buttons(["Cancel", "Delete"])
        dlg.set_default_button(0)
        dlg.set_cancel_button(0)
        dlg.choose(self, None, lambda d, r: self._on_delete_response(d, r, row))

    def _on_delete_response(self, dlg, result, row):
        try:
            choice = dlg.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return
        try:
            importer.delete_pdf(self.conn, row["pdf_path"])
        except Exception as e:
            print("delete failed:", e)
            self.status.set_text("Delete failed: {}".format(e))
            return
        self.status.set_text("Deleted: " + os.path.basename(row["pdf_path"]))
        self._reload(self.search.get_text() or None)

    def _open_rename_dialog(self, row):
        old_path = row["pdf_path"]
        win = Gtk.Window(transient_for=self, modal=True)
        win.set_title("Rename PDF")
        win.set_default_size(520, 120)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        lbl = Gtk.Label(label="New filename (in same folder):")
        lbl.set_halign(Gtk.Align.START)
        box.append(lbl)

        entry = Gtk.Entry()
        entry.set_text(os.path.basename(old_path))
        entry.set_hexpand(True)
        box.append(entry)

        msg = Gtk.Label()
        msg.set_halign(Gtk.Align.START)
        box.append(msg)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        rename_b = Gtk.Button(label="Rename")
        rename_b.add_css_class("suggested-action")
        btns.append(cancel)
        btns.append(rename_b)
        box.append(btns)

        def do_rename(_b):
            new_basename = entry.get_text().strip()
            if not new_basename:
                msg.set_markup("<small>Name cannot be empty.</small>")
                return
            if not new_basename.lower().endswith(".pdf"):
                new_basename += ".pdf"
            new_path = os.path.join(os.path.dirname(old_path), new_basename)
            if new_path == old_path:
                win.close()
                return
            if os.path.exists(new_path):
                msg.set_markup("<small>A file with that name already exists.</small>")
                return
            try:
                importer.rename_pdf(self.conn, old_path, new_path)
            except Exception as e:
                msg.set_markup("<small>Rename failed: {}</small>".format(
                    GLib.markup_escape_text(str(e))))
                return
            win.close()
            self.status.set_text("Renamed to " + new_basename)
            self._reload(self.search.get_text() or None)

        cancel.connect("clicked", lambda _b: win.close())
        rename_b.connect("clicked", do_rename)
        entry.connect("activate", do_rename)

        win.set_child(box)
        win.present()

    # --- Background citation refresh ------------------------------------

    def _citation_refresher(self, max_age_days=30, pause_seconds=3.0):
        """Slowly refresh citation counts that are missing or older than
        max_age_days. Runs once per browser session (a daemon thread).
        On a successful fetch, citations_fetched is bumped to today.
        On failure, the date is left unchanged so we'll retry next
        session, but we record the path in a per-session set so we
        don't pummel a failing endpoint within one run."""
        if self._cit_stop.wait(2.0):
            return
        rows = index.stale_citation_rows(self.conn, max_age_days=max_age_days)
        if not rows:
            return
        for row in rows:
            if self._cit_stop.is_set():
                return
            if row["pdf_path"] in self._cit_failed_session:
                continue
            doi = row.get("doi")
            if not doi:
                continue
            n, src, kw = metrics.fetch_metrics(doi)
            if n is None:
                self._cit_failed_session.add(row["pdf_path"])
            else:
                today = metrics.today_iso()
                try:
                    rec = sidecar.read(row["sidecar_path"])
                    rec["citations"] = n
                    rec["citations_source"] = src
                    rec["citations_fetched"] = today
                    if kw:
                        rec["auto_keywords"] = kw
                    sidecar.write(row["sidecar_path"], rec)
                    # Push the updated record into the index too so the
                    # next reload picks up the new keywords.
                    th = row.get("thumb_path")
                    mtime = os.path.getmtime(row["sidecar_path"])
                    index.upsert(self.conn, row["pdf_path"],
                                 row["sidecar_path"], th, rec, mtime)
                except Exception as e:
                    print("citation sidecar write failed:", e)
                    index.update_citations(self.conn, row["pdf_path"],
                                           n, src, today)
                GLib.idle_add(self._refresh_visible_row,
                              row["pdf_path"], n)
            if self._cit_stop.wait(pause_seconds):
                return

    def _refresh_visible_row(self, pdf_path, count):
        # Cheap: just rebuild the list. (Could rebuild a single card later.)
        self._reload(self.search.get_text() or None)
        return False

    # --- Drag-and-drop --------------------------------------------------

    def _on_drop(self, _target, value, _x, _y):
        try:
            files = value.get_files()
        except Exception:
            return False
        paths = []
        for f in files:
            p = f.get_path() if f else None
            if p and p.lower().endswith(".pdf") and os.path.isfile(p):
                paths.append(p)
        if not paths:
            self.status.set_text("Drop: no PDFs found")
            return False
        self.status.set_text("Importing {} dropped file(s)...".format(len(paths)))
        threading.Thread(target=self._do_drop_import,
                         args=(paths,), daemon=True).start()
        return True

    def _do_drop_import(self, paths):
        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        results = {"imported": [], "duplicate": [], "exists": [], "error": []}
        for src in paths:
            target = os.path.join(LIBRARY_ROOT, os.path.basename(src))
            if os.path.realpath(src) == os.path.realpath(target):
                # Already in the library — just (re)import in place.
                try:
                    rec, status = importer.import_pdf(self.conn, target)
                    results.setdefault(status, []).append((src, target, rec))
                except Exception as e:
                    results["error"].append((src, target, str(e)))
                continue
            if os.path.exists(target):
                results["exists"].append((src, target, None))
                continue
            try:
                shutil.copy2(src, target)
            except Exception as e:
                results["error"].append((src, target, str(e)))
                continue
            try:
                rec, status = importer.import_pdf(self.conn, target)
            except Exception as e:
                results["error"].append((src, target, str(e)))
                try: os.remove(target)
                except Exception: pass
                continue
            if status == "duplicate":
                # Drop the copy; the library already had it.
                try: os.remove(target)
                except Exception: pass
                results["duplicate"].append((src, target, rec))
            else:
                results["imported"].append((src, target, rec))
        GLib.idle_add(self._on_drop_done, results)

    def _on_drop_done(self, results):
        # Refresh the visible list to show newly-imported entries.
        self._reload(self.search.get_text() or None)
        bits = []
        if results["imported"]:
            bits.append("imported {}".format(len(results["imported"])))
        if results["duplicate"]:
            bits.append("duplicate {}".format(len(results["duplicate"])))
        if results["exists"]:
            bits.append("name-clash {}".format(len(results["exists"])))
        if results["error"]:
            bits.append("error {}".format(len(results["error"])))
        if not bits:
            bits.append("nothing to do")
        self.status.set_text("Drop: " + ", ".join(bits))
        for src, target, rec in results["error"]:
            print("drop error:", src, "->", target, ":", rec)
        for src, target, rec in results["duplicate"]:
            existing = rec.get("pdf_path") if rec else "?"
            print("drop duplicate: {} matches existing {}".format(src, existing))
        return False

    def _reload(self, query):
        child = self.results.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.results.remove(child)
            child = nxt
        rows = index.search(self.conn, query)
        on_saved = lambda: self._reload(self.search.get_text() or None)
        for r in rows:
            self.results.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            self.results.append(make_card(r, self, self.conn, on_saved))
        self.status.set_text("{} entries".format(len(rows)))


def main(argv):
    conn = index.open_db()
    app = Gtk.Application(application_id="org.coot.pdforg")

    def on_activate(app):
        win = BrowserWindow(app, conn)
        win.present()

    app.connect("activate", on_activate)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
