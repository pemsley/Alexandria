#!/usr/bin/env python3
"""Alexandria — browser for the PDF library and OpenAlex

Reads from the local SQLite index; sidecar JSON files (next to each PDF)
are the source of truth."""

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

from . import (index, edit_dialog, importer, metrics, sidecar, extract,
               viewer, marks_config, watcher as watcher_mod, author_works,
               bibtex_import, bibtex_export)

LIBRARY_ROOT = os.environ.get(
    "PDFORG_LIBRARY", os.path.expanduser("~/pdfs"))


# Display flags. Future plan: surface these via a "Display Options"
# popover with Compact / Standard / Verbose presets. For now they are
# module-level constants and default to a quiet card.
display_auto_keywords = False


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
    """A small auto-keyword (OpenAlex concept) shown beneath a card.
    Plain label with theme-aware dim styling — `alpha` follows the
    theme foreground so it reads in both light and dark modes."""
    lbl = Gtk.Label()
    lbl.set_markup(
        '<small><span alpha="60%">{}</span></small>'.format(
            GLib.markup_escape_text(text)))
    lbl.set_valign(Gtk.Align.CENTER)
    lbl.set_margin_start(2)
    lbl.set_margin_end(2)
    return lbl


def make_mark_dropdown(items):
    """items: list of (label, hex_color_or_None) tuples. Returns a
    Gtk.DropDown whose visible items show a coloured ● before the
    label when a color is given. The same factory is used for the
    collapsed (selected) item and the popup list."""
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
    return Gtk.DropDown(model=sl, factory=factory)


_MARK_COLORS = {
    "red":    "#cc3333",
    "orange": "#ee8800",
    "green":  "#33aa33",
    "cyan":   "#33aaaa",
}


def make_mark_badge(mark, labels=None):
    """A small framed coloured-circle chip for the user 'Mark' field.
    Returns None when no mark is set. `labels` is the marks-config
    dict (color → user label); when set, the tooltip uses the label."""
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
    user_label = marks_config.label_for(mark, labels) if labels else ""
    lbl.set_tooltip_text("Mark: " + (user_label or mark))
    frame.set_child(lbl)
    return frame


def make_preprint_badge():
    """A small 'PRE' chip to flag preprint entries (no published
    version known)."""
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


def _published_in_library(conn, doi):
    """Return the indexed row whose `doi` matches (case-insensitive),
    or None."""
    if not doi:
        return None
    try:
        cur = conn.execute(
            "SELECT pdf_path, title FROM papers WHERE LOWER(doi)=? LIMIT 1",
            (doi.lower(),))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def make_preprint_status(row, conn, parent_window):
    """Build the preprint chip(s) for a card. Returns one widget — a
    bare PRE badge, or a clickable button reflecting the published-
    version state. Returns None if not a preprint."""
    if not is_preprint(row):
        return None

    pv = None
    pv_json = row["published_version_json"] if "published_version_json" in row.keys() else None
    if pv_json:
        try:
            pv = json.loads(pv_json)
        except (TypeError, ValueError):
            pv = None

    if not pv:
        return make_preprint_badge()

    pub_doi = (pv.get("doi") or "").lower()
    journal = pv.get("journal") or "(journal)"
    year = pv.get("year")
    in_lib = _published_in_library(conn, pub_doi)

    btn = Gtk.Button()
    btn.add_css_class("flat")
    btn.set_valign(Gtk.Align.CENTER)
    inner = Gtk.Label()
    label_year = " {}".format(year) if year else ""
    if in_lib:
        # Green: we have it.
        inner.set_markup(
            '<span foreground="#33aa33" weight="bold"><small>'
            '✓ in library</small></span>')
        btn.set_tooltip_text(
            "Published as «{}» in {}{}.\n"
            "Click to navigate.".format(
                pv.get("title") or "(untitled)", journal, label_year))
        btn.connect(
            "clicked",
            lambda _b, d=pub_doi: parent_window._navigate_to_doi(d))
    else:
        # Orange: we know about it but don't have it.
        inner.set_markup(
            '<span foreground="#cc6600" weight="bold"><small>'
            '📰 published — Add</small></span>')
        btn.set_tooltip_text(
            "Published as «{}» in {}{}.\n"
            "Click to download into the library.".format(
                pv.get("title") or "(untitled)", journal, label_year))
        btn.connect(
            "clicked",
            lambda _b, p=pv, b=btn:
                parent_window._add_published_version(p, b))
    btn.set_child(inner)
    return btn


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


# Colour-coded sparkline tiers, indexed by peak citations-per-year.
# Saturated hues mixed roughly half-and-half with mid-grey so they stay
# visually quiet on the card. Below 10/yr we just use the theme's
# foreground colour (no signal to communicate).
_SPARKLINE_TIERS = (
    (10, None),                  # < 10  → theme grey
    (20, (0x44, 0xaa, 0xaa)),    # < 20  → muted cyan
    (40, (0x44, 0xaa, 0x44)),    # < 40  → muted green
    (None, (0xaa, 0xaa, 0x44)),  # else  → muted yellow
)


def _sparkline_colour(peak):
    """Return (r, g, b) ints in 0..255 for a peak yearly count, or None
    to mean "use the theme foreground"."""
    for threshold, rgb in _SPARKLINE_TIERS:
        if threshold is None or peak < threshold:
            return rgb


def make_citation_sparkline(cby):
    """Tiny per-year-citations bar chart, or None if not worth drawing.

    `cby` is a list of {year, count} dicts (oldest-first), as produced
    by metrics._openalex_metrics. Returns a Gtk.DrawingArea sized to
    sit inline beside the 'cited Nx' label, or None if there's too
    little data."""
    if not cby or len(cby) < 2:
        return None
    peak = max(r.get("count") or 0 for r in cby)
    if peak < 2:
        return None

    width, height = 90, 22
    area = Gtk.DrawingArea()
    area.set_content_width(width)
    area.set_content_height(height)
    area.set_valign(Gtk.Align.CENTER)

    # Tooltip: "2018: 12  ·  2019: 24  ·  …"
    tip = "  ·  ".join(
        "{}: {}".format(r["year"], r.get("count") or 0) for r in cby)
    area.set_tooltip_text(tip)

    rgb = _sparkline_colour(peak)

    def _draw(_a, cr, w, h):
        n = len(cby)
        gap = 1
        bw = max(1.5, (w - (n - 1) * gap) / n)
        if rgb is None:
            fg = area.get_style_context().get_color()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        else:
            cr.set_source_rgba(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 0.85)
        for i, r in enumerate(cby):
            c = r.get("count") or 0
            if c <= 0:
                continue
            bh = (h - 2) * (c / peak)
            x = i * (bw + gap)
            y = h - 1 - bh
            cr.rectangle(x, y, bw, bh)
            cr.fill()
        # Faint baseline (always theme-coloured so it sits well on
        # both light and dark backgrounds).
        fg = area.get_style_context().get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
        cr.set_line_width(1.0)
        cr.move_to(0, h - 0.5)
        cr.line_to(w, h - 0.5)
        cr.stroke()

    area.set_draw_func(_draw)
    return area


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


def make_card(row, parent_window, conn, on_saved, mark_labels=None):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_start(8)
    box.set_margin_end(8)
    box.set_margin_top(6)
    box.set_margin_bottom(6)

    is_ghost = sidecar.is_ghost_path(row["pdf_path"])

    img = Gtk.Image()
    img.set_pixel_size(120)
    # Ghost (BibTeX-only) entries have no PDF and no thumbnail; show a
    # generic "no document" icon to make the difference obvious.
    img.set_from_icon_name("text-x-generic-symbolic" if is_ghost
                           else "application-pdf")
    if (not is_ghost and row["thumb_path"]
            and os.path.isfile(row["thumb_path"])):
        try:
            tex = Gdk.Texture.new_from_file(Gio.File.new_for_path(row["thumb_path"]))
            img.set_from_paintable(tex)
        except Exception:
            pass
    frame = Gtk.Frame()
    frame.set_size_request(130, 160)
    frame.set_child(img)
    if not is_ghost:
        frame.set_cursor_from_name("pointer")
        frame.set_tooltip_text("View PDF")
        click = Gtk.GestureClick.new()
        click.set_button(1)
        click.connect(
            "released",
            lambda *_: viewer.open_viewer(parent_window, row["pdf_path"],
                                          row["sidecar_path"]))
        frame.add_controller(click)
    else:
        frame.set_tooltip_text(
            "BibTeX-only entry — drop a PDF here to attach it")
        # Drop target: a PDF dropped onto this thumbnail is attached
        # to the ghost via bibtex_import.attach_pdf_to_ghost().
        ghost_drop = Gtk.DropTarget.new(Gdk.FileList,
                                        Gdk.DragAction.COPY)
        ghost_drop.connect(
            "drop",
            lambda t, value, x, y, r=row:
                parent_window._on_ghost_drop(t, value, r))
        frame.add_controller(ghost_drop)
    box.append(frame)

    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    text.set_hexpand(True)

    btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    if not is_ghost:
        open_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        open_btn.set_tooltip_text("View PDF")
        open_btn.connect(
            "clicked",
            lambda _b: viewer.open_viewer(parent_window, row["pdf_path"],
                                          row["sidecar_path"]))
        btn_row.append(open_btn)
    else:
        # "Get PDF" — try to download an OA copy via OpenAlex's
        # best_oa_location (and its mirrors), and on success run the
        # ghost-merge automatically. If nothing OA is downloadable
        # (paywall, Cloudflare, no OA URL), fall back to opening the
        # DOI in the browser so the user can save and drag in.
        get_btn = Gtk.Button.new_from_icon_name("folder-download-symbolic")
        get_btn.set_tooltip_text(
            "Get PDF — try downloading an open-access copy via "
            "OpenAlex; on failure, open the DOI in your browser.")
        get_btn.connect(
            "clicked",
            lambda _b, r=row: parent_window._on_get_pdf(r))
        btn_row.append(get_btn)
    edit_btn = Gtk.Button.new_from_icon_name("document-properties-symbolic")
    edit_btn.set_tooltip_text("Edit metadata")
    edit_btn.connect(
        "clicked",
        lambda _b: edit_dialog.open_editor(
            parent_window, conn,
            row["pdf_path"], row["sidecar_path"], on_saved))
    btn_row.append(edit_btn)
    if not is_ghost:
        rename_btn = Gtk.Button.new_from_icon_name("edit-rename-symbolic")
        rename_btn.set_tooltip_text("Rename PDF")
        rename_btn.connect("clicked",
                           lambda _b: parent_window._open_rename_dialog(row))
        btn_row.append(rename_btn)
    if row["doi"]:
        related_btn = Gtk.Button.new_from_icon_name("view-more-symbolic")
        related_btn.set_tooltip_text(
            "Related works (OpenAlex)\n"
            "Note: similarity is fuzzy and topic-based, "
            "results can be loose")
        related_btn.connect(
            "clicked",
            lambda b: parent_window._open_related_popover(b, row))
        btn_row.append(related_btn)
        cited_by_btn = Gtk.Button.new_from_icon_name("mail-forward-symbolic")
        cited_by_btn.set_tooltip_text(
            "Cited by — papers that cite this one (OpenAlex)\n"
            "Shows the most recent and the most-cited citing papers")
        cited_by_btn.connect(
            "clicked",
            lambda b: parent_window._open_cited_by_popover(b, row))
        btn_row.append(cited_by_btn)
    if row["abstract"]:
        abstract_btn = Gtk.Button.new_from_icon_name(
            "format-justify-fill-symbolic")
        abstract_btn.set_tooltip_text("Show abstract")
        abstract_btn.connect(
            "clicked",
            lambda b: parent_window._open_abstract_popover(b, row))
        btn_row.append(abstract_btn)
    delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
    delete_btn.set_tooltip_text("Delete PDF from library")
    delete_btn.connect("clicked",
                       lambda _b: parent_window._confirm_delete(row))
    btn_row.append(delete_btn)
    path_lbl = Gtk.Label()
    if is_ghost:
        # Show "BibTeX entry: <key>" instead of `bibtex:<key>` directly.
        key = row["pdf_path"].split(":", 1)[1] if ":" in row["pdf_path"] else "?"
        path_lbl.set_markup(
            '<small><span alpha="65%">BibTeX entry: </span>'
            '<tt>{}</tt></small>'.format(GLib.markup_escape_text(key)))
    else:
        path_lbl.set_markup("<small><tt>{}</tt></small>".format(
            GLib.markup_escape_text(row["pdf_path"])))
    path_lbl.set_halign(Gtk.Align.START)
    path_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    path_lbl.set_max_width_chars(70)
    path_lbl.set_selectable(True)
    btn_row.append(path_lbl)
    text.append(btn_row)

    title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    mark_badge = make_mark_badge(row["mark"], labels=mark_labels)
    if mark_badge is not None:
        title_row.append(mark_badge)
    pre_chip = make_preprint_status(row, conn, parent_window)
    if pre_chip is not None:
        title_row.append(pre_chip)
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

    # Authors row: clickable, opens a popover with the full list and
    # per-author actions. Styled as a "link" so the user sees it's
    # different from a plain label.
    n_authors = 0
    try:
        n_authors = len(json.loads(row["authors_json"] or "[]"))
    except (TypeError, ValueError):
        pass
    auth_text = authors_str(row["authors_json"])
    if len(auth_text) > 120:
        auth_text = auth_text[:117] + "..."
    suffix = "  ▾"
    if n_authors > 4 and "..." not in auth_text:
        # Already showed everyone but there are >4 — keep the caret.
        pass
    if "..." in auth_text:
        suffix = "  ({} authors)  ▾".format(n_authors)
    auth_btn = Gtk.Button()
    auth_btn.add_css_class("flat")
    auth_btn.add_css_class("pdforg-author-link")
    auth_btn.set_halign(Gtk.Align.START)
    auth_btn.set_has_frame(False)
    auth_btn.set_tooltip_text("Click for full author list and actions")
    auth_inner = Gtk.Label()
    auth_inner.set_markup(
        "<small><span underline='single'>{}</span>{}</small>".format(
            GLib.markup_escape_text(auth_text),
            GLib.markup_escape_text(suffix)))
    auth_inner.set_halign(Gtk.Align.START)
    auth_inner.set_ellipsize(Pango.EllipsizeMode.END)
    auth_inner.set_max_width_chars(80)
    auth_btn.set_child(auth_inner)
    auth_btn.connect("clicked",
                     lambda b: parent_window._open_authors_popover(b, row))
    text.append(auth_btn)

    yj_bits = []
    if row["year"]:
        yj_bits.append(str(row["year"]))
    if row["journal"]:
        yj_bits.append(row["journal"])
    if row["citations"] is not None:
        yj_bits.append("cited {}×".format(row["citations"]))
    if yj_bits:
        yj_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        yj_row.set_halign(Gtk.Align.START)
        yj = Gtk.Label()
        yj.set_markup("<small><i>{}</i></small>".format(
            GLib.markup_escape_text("  ·  ".join(yj_bits))))
        yj.set_halign(Gtk.Align.START)
        yj_row.append(yj)
        # Per-year citations sparkline, when we have OpenAlex data.
        cby_json = (row["citations_by_year_json"]
                    if "citations_by_year_json" in row.keys() else None)
        try:
            cby = json.loads(cby_json or "[]")
        except (TypeError, ValueError):
            cby = []
        spark = make_citation_sparkline(cby)
        if spark is not None:
            yj_row.append(spark)
        text.append(yj_row)

    stars = citation_stars_markup(row["citations"])
    if stars:
        star_lbl = Gtk.Label()
        star_lbl.set_markup("<small>{}</small>".format(stars))
        star_lbl.set_halign(Gtk.Align.START)
        text.append(star_lbl)

    # Auto-keywords (OpenAlex concepts). Hidden by default — they bulk
    # the card up without being especially actionable. Will be revealed
    # by a future "Display Options → Verbose" preset.
    if display_auto_keywords:
        auto_kw_json = (row["auto_keywords_json"]
                        if "auto_keywords_json" in row.keys() else None)
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
        self.set_title("Alexandria")
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
        import_bib_btn = Gtk.Button(label="Import BibTeX…")
        import_bib_btn.connect("clicked", self._on_import_bibtex)
        toolbar.append(import_bib_btn)
        export_bib_btn = Gtk.Button(label="Export BibTeX…")
        export_bib_btn.set_tooltip_text(
            "Save the currently visible entries (search + mark filter) "
            "as a .bib file")
        export_bib_btn.connect("clicked", self._on_export_bibtex)
        toolbar.append(export_bib_btn)
        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text("Search title / authors / DOI / journal")
        self.search.connect("search-changed", self._on_search)
        toolbar.append(self.search)

        # Mark filter dropdown — built from the user's marks-config labels.
        self.mark_labels = marks_config.load()
        self._MARK_FILTER_VALUES = [None, "red", "orange", "green", "cyan",
                                    index.MARK_FILTER_NONE]
        self._toolbar_box = toolbar  # remember so we can rebuild the dropdown
        self.mark_filter_dd = self._build_mark_filter_dd()
        toolbar.append(self.mark_filter_dd)

        marks_prefs_btn = Gtk.Button.new_from_icon_name(
            "preferences-system-symbolic")
        marks_prefs_btn.set_tooltip_text("Edit mark labels…")
        marks_prefs_btn.connect("clicked", self._open_marks_prefs)
        toolbar.append(marks_prefs_btn)

        self.status = Gtk.Label()
        self.status.set_halign(Gtk.Align.END)
        self.status.set_use_markup(True)
        # Custom URI scheme `alex:show-top` is intercepted in
        # _on_status_link to scroll the cards list to the top
        # (where freshly-imported entries sit, post-sort).
        self.status.connect("activate-link", self._on_status_link)
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

        self.results_scrolled = Gtk.ScrolledWindow()
        self.results_scrolled.set_vexpand(True)
        self.results_scrolled.set_hexpand(True)
        self.results_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC,
                                         Gtk.PolicyType.AUTOMATIC)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.results_scrolled.set_child(self.results)
        outer.append(self.results_scrolled)

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

        # GFileMonitor-based library watcher: react to external file
        # changes in LIBRARY_ROOT (drops via Files / cp / sync tools,
        # plus sidecar rewrites from `pdforg-import --refresh`).
        self._reload_timer_id = None
        self._pending_reload_status = ""
        self.library_watcher = watcher_mod.LibraryWatcher(
            self.conn, LIBRARY_ROOT,
            on_change_cb=self._on_watcher_change)
        self.library_watcher.start()
        self.library_watcher.reconcile_startup()
        self.connect("close-request", self._on_close_request)

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

    # --- BibTeX import ------------------------------------------------

    def _on_import_bibtex(self, _btn):
        if self._import_busy:
            self.status.set_text("Import already running")
            return
        dlg = Gtk.FileDialog()
        dlg.set_title("Import a .bib file")
        bib_filter = Gtk.FileFilter()
        bib_filter.set_name("BibTeX (*.bib)")
        bib_filter.add_pattern("*.bib")
        bib_filter.add_pattern("*.bibtex")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(bib_filter)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        filters.append(all_filter)
        dlg.set_filters(filters)
        dlg.set_default_filter(bib_filter)
        dlg.open(self, None, self._on_bib_chosen)

    def _on_bib_chosen(self, dlg, result):
        try:
            f = dlg.open_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        self._start_import_bib(path)

    def _start_import_bib(self, bib_path):
        self._show_progress(
            "Reading {}...".format(os.path.basename(bib_path)), 0.0)
        self._import_busy = True
        threading.Thread(target=self._do_import_bib,
                         args=(bib_path,), daemon=True).start()

    def _do_import_bib(self, bib_path):
        def progress(i, n, key, status):
            frac = (i / n) if n else 0.0
            label = "{}/{}  {}  ({})".format(i, n, key or "?", status)
            GLib.idle_add(self._show_progress, label, frac)

        try:
            counts = bibtex_import.import_bib(
                self.conn, bib_path, LIBRARY_ROOT, on_progress=progress)
        except Exception as e:
            print("BibTeX import failed:", e)
            counts = None
        GLib.idle_add(self._do_import_bib_done, counts)

    def _do_import_bib_done(self, counts):
        self._import_busy = False
        self._hide_progress()
        if counts is None:
            self.status.set_text("BibTeX import failed (see terminal)")
        else:
            msg = "BibTeX: {} imported, {} ghost, {} duplicate, {} errors".format(
                counts["imported"], counts["ghost"],
                counts["duplicate"], counts["error"])
            n_new = counts["imported"] + counts["ghost"]
            if n_new:
                self._set_status_with_show(msg)
            else:
                self.status.set_text(msg)
        self._reload(self.search.get_text() or None)
        return False

    # --- BibTeX export ------------------------------------------------

    def _on_export_bibtex(self, _btn):
        dlg = Gtk.FileDialog()
        dlg.set_title("Export BibTeX")
        dlg.set_initial_name("alexandria-export.bib")
        bib_filter = Gtk.FileFilter()
        bib_filter.set_name("BibTeX (*.bib)")
        bib_filter.add_pattern("*.bib")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(bib_filter)
        dlg.set_filters(filters)
        dlg.set_default_filter(bib_filter)
        dlg.save(self, None, self._on_bib_save_chosen)

    def _on_bib_save_chosen(self, dlg, result):
        try:
            f = dlg.save_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        if not path.lower().endswith(".bib"):
            path += ".bib"

        # Export *the currently visible rows* — search text + mark
        # filter applied. That makes "filtered export" the natural
        # default; users who want everything just clear filters first.
        query = self.search.get_text() or None
        mark_filter = self._MARK_FILTER_VALUES[
            self.mark_filter_dd.get_selected()]
        rows = index.search(self.conn, query, mark_filter=mark_filter)

        try:
            written, skipped = bibtex_export.export_rows_to_file(rows, path)
        except Exception as e:
            print("BibTeX export failed:", e)
            self.status.set_text("Export failed: {}".format(e))
            return

        msg = "Exported {} entries to {}".format(
            written, os.path.basename(path))
        if skipped:
            msg += " ({} skipped — sidecar missing)".format(skipped)
        self.status.set_text(msg)

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
        is_ghost = sidecar.is_ghost_path(row["pdf_path"])
        dlg = Gtk.AlertDialog()
        dlg.set_modal(True)
        if is_ghost:
            dlg.set_message("Delete this BibTeX-only entry?")
            dlg.set_detail(
                "This will remove the metadata sidecar and the index "
                "row for «{}». No PDF on disk will be touched.".format(
                    row["title"] or row["pdf_path"]))
        else:
            dlg.set_message("Delete this PDF from the library?")
            dlg.set_detail(
                "This will remove:\n  {}\n  + sidecar + thumbnail".format(
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
            n, src, kw, abstract, authorships, cby = metrics.fetch_metrics(doi)
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
                    if abstract:
                        rec["abstract"] = abstract
                    if authorships:
                        rec["authorships"] = authorships
                        oa_names = [a["name"] for a in authorships if a.get("name")]
                        if oa_names:
                            rec["authors"] = oa_names
                    if cby:
                        rec["citations_by_year"] = cby
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

    def _on_ghost_drop(self, _target, value, ghost_row):
        """A PDF was dropped onto the thumbnail of a BibTeX-only card.
        Route it through bibtex_import.attach_pdf_to_ghost — which
        does its own DOI match check, copies the PDF in, runs the
        full import, merges the ghost's curation onto the new
        sidecar, and removes the ghost."""
        try:
            files = value.get_files()
        except Exception:
            return False
        src_path = None
        for f in files:
            p = f.get_path() if f else None
            if p and p.lower().endswith(".pdf") and os.path.isfile(p):
                src_path = p
                break
        if not src_path:
            self.status.set_text("Ghost-drop: not a PDF")
            return False
        self.status.set_text("Attaching {}...".format(os.path.basename(src_path)))
        threading.Thread(
            target=self._do_ghost_drop,
            args=(dict(ghost_row), src_path),
            daemon=True).start()
        return True

    def _do_ghost_drop(self, ghost_row, src_path):
        try:
            new_path, status, msg = bibtex_import.attach_pdf_to_ghost(
                self.conn, ghost_row, src_path, LIBRARY_ROOT)
        except Exception as e:
            print("attach_pdf_to_ghost failed:", e)
            new_path, status, msg = None, "error", str(e)
        GLib.idle_add(self._on_ghost_drop_done, status, msg)

    def _on_ghost_drop_done(self, status, msg):
        self.status.set_text(msg or status)
        self._reload(self.search.get_text() or None)
        return False

    def _ghost_for_doi(self, doi):
        """Find a ghost (BibTeX-only) row whose DOI matches `doi`,
        or None. Used to route Path C — auto-merge when a dropped
        PDF's DOI matches an existing ghost."""
        if not doi:
            return None
        ndoi = index.normalize_doi(doi)
        if not ndoi:
            return None
        try:
            cur = self.conn.execute(
                "SELECT * FROM papers WHERE LOWER(doi) = ?",
                (ndoi.lower(),))
            for row in cur:
                d = dict(row)
                if sidecar.is_ghost_path(d["pdf_path"]):
                    return d
        except Exception:
            pass
        return None

    def _do_drop_import(self, paths):
        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        results = {"imported": [], "duplicate": [], "exists": [],
                   "error": [], "merged": []}
        for src in paths:
            # Path C — auto-merge: if the dropped PDF's DOI matches a
            # ghost in the library, run the merge flow directly so
            # the BibTeX provenance is preserved.
            try:
                src_doi = extract._scan_doi_in_pages(src, max_pages=4)
            except Exception:
                src_doi = None
            ghost = self._ghost_for_doi(src_doi) if src_doi else None
            if ghost:
                try:
                    new_path, gstatus, gmsg = (
                        bibtex_import.attach_pdf_to_ghost(
                            self.conn, ghost, src, LIBRARY_ROOT))
                except Exception as e:
                    results["error"].append((src, None, str(e)))
                    continue
                if gstatus == "merged":
                    results["merged"].append((src, new_path, ghost))
                else:
                    results["error"].append((src, None, gmsg))
                continue

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
        if results.get("merged"):
            bits.append("attached to BibTeX {}".format(len(results["merged"])))
        if results["duplicate"]:
            bits.append("duplicate {}".format(len(results["duplicate"])))
        if results["exists"]:
            bits.append("name-clash {}".format(len(results["exists"])))
        if results["error"]:
            bits.append("error {}".format(len(results["error"])))
        if not bits:
            bits.append("nothing to do")
        # Newly imported entries land at the top of the list (added_date
        # DESC). Offer a "show ↗" link in the status so the user can
        # jump there from anywhere in a long scroll.
        n_new = len(results["imported"]) + len(results.get("merged") or [])
        msg = "Drop: " + ", ".join(bits)
        if n_new:
            self._set_status_with_show(msg)
        else:
            self.status.set_text(msg)
        for src, target, rec in results["error"]:
            print("drop error:", src, "->", target, ":", rec)
        for src, target, rec in results["duplicate"]:
            existing = rec.get("pdf_path") if rec else "?"
            print("drop duplicate: {} matches existing {}".format(src, existing))
        return False

    # --- Status-line "show ↗" affordance ------------------------------

    def _set_status_with_show(self, message):
        """Set the status label to `message` followed by a clickable
        'show ↗' link. The link scrolls the cards list to the top —
        which, with the added-date sort, is where newly-imported
        entries live."""
        self.status.set_markup(
            '{}  <a href="alex:show-top">show ↗</a>'.format(
                GLib.markup_escape_text(message)))

    def _on_status_link(self, _label, uri):
        """Intercept clicks on `<a href="alex:...">` links in the
        status bar. Returning True stops Gtk from trying to open it
        with xdg-open."""
        if uri == "alex:show-top":
            self._scroll_results_to_top()
            return True
        return False

    def _scroll_results_to_top(self):
        try:
            adj = self.results_scrolled.get_vadjustment()
            adj.set_value(0)
        except Exception:
            pass

    def _reload(self, query):
        child = self.results.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.results.remove(child)
            child = nxt
        mark_filter = self._MARK_FILTER_VALUES[
            self.mark_filter_dd.get_selected()]
        rows = index.search(self.conn, query, mark_filter=mark_filter)
        on_saved = lambda: self._reload(self.search.get_text() or None)
        for r in rows:
            self.results.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            self.results.append(make_card(r, self, self.conn, on_saved,
                                          mark_labels=self.mark_labels))
        self.status.set_text("{} entries".format(len(rows)))

    def _on_mark_filter_changed(self, _dd, _pspec):
        self._reload(self.search.get_text() or None)

    # --- Preprint → published-version actions -------------------------

    def _on_get_pdf(self, row):
        """Ghost-card "Get PDF": ask OpenAlex for OA pdf URLs for the
        entry's DOI, try them in order via our existing downloader, and
        on success route through the ghost-merge flow so the BibTeX
        provenance is preserved on the resulting normal entry. If
        nothing OA is available — or every download is blocked
        (Cloudflare, paywall HTML, etc.) — fall back to opening the
        DOI in the system browser as before."""
        doi = row["doi"]
        if not doi:
            self.status.set_text(
                "No DOI on this entry — edit metadata to add one")
            return
        self.status.set_text("Looking for an open-access PDF…")
        threading.Thread(
            target=self._do_get_pdf,
            args=(dict(row), doi),
            daemon=True).start()

    def _do_get_pdf(self, row, doi):
        import tempfile
        import urllib.parse as _up
        from . import author_works as _aw

        url = ("https://api.openalex.org/works/doi:"
               + _up.quote(doi, safe="")
               + "?mailto=" + _up.quote(metrics.OPENALEX_MAILTO))
        data = metrics._http_get_json(
            url,
            headers={"User-Agent": metrics.OPENALEX_UA,
                     "Accept": "application/json"},
            timeout=15)
        if not data:
            GLib.idle_add(self._get_pdf_fallback, doi,
                          "OpenAlex lookup failed")
            return

        # Collect every OA pdf_url (best_oa_location first, then mirrors).
        bol = data.get("best_oa_location") or {}
        pdf_urls = []
        if bol.get("pdf_url"):
            pdf_urls.append(bol["pdf_url"])
        for loc in (data.get("locations") or []):
            if not loc.get("is_oa"):
                continue
            u = loc.get("pdf_url")
            if u and u not in pdf_urls:
                pdf_urls.append(u)
        if not pdf_urls:
            GLib.idle_add(
                self._get_pdf_fallback, doi,
                "no OA PDF URL known to OpenAlex")
            return

        # Download into a tmp file. Magic-byte check + Cloudflare
        # detection are inside _download_pdf.
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        last_msg = ""
        ok = False
        for u in pdf_urls:
            ok, last_msg = _aw._download_pdf(u, tmp_path)
            if ok:
                break
        if not ok:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            GLib.idle_add(self._get_pdf_fallback, doi, last_msg)
            return

        # Attach to the ghost: copy into LIBRARY_ROOT (named for the
        # bibtex_key), run import_pdf, merge the ghost's curation,
        # remove the ghost.
        try:
            new_path, status, msg = bibtex_import.attach_pdf_to_ghost(
                self.conn, row, tmp_path, LIBRARY_ROOT)
        except Exception as e:
            new_path, status, msg = None, "error", str(e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        GLib.idle_add(self._get_pdf_done, status, msg)

    def _get_pdf_fallback(self, doi, error_msg):
        """No OA copy is downloadable; open DOI in browser and let the
        user save+drag the PDF in."""
        open_pdf("https://doi.org/" + doi)
        self.status.set_text(
            "Direct download failed ({}) — opened DOI in browser; "
            "save and drag the PDF onto the card".format(error_msg))
        return False

    def _get_pdf_done(self, status, msg):
        self.status.set_text(msg or status)
        self._reload(self.search.get_text() or None)
        return False

    def _navigate_to_doi(self, doi):
        """Filter the visible list to the given DOI (FTS prefix-matches it)."""
        if not doi:
            return
        self.search.set_text(doi)
        self.search.grab_focus()

    def _add_published_version(self, pv, btn):
        """Download the published-version PDF (using OpenAlex to resolve
        OA URLs for the journal DOI) and import it into the library."""
        doi = pv.get("doi")
        if not doi:
            return
        # Already in library? (race against the user clicking twice.)
        if _published_in_library(self.conn, doi):
            btn.set_label("✓ in library")
            btn.set_sensitive(False)
            return
        btn.set_sensitive(False)
        btn.set_label("Looking up…")
        threading.Thread(
            target=self._do_add_published_version,
            args=(doi, btn),
            daemon=True,
        ).start()

    def _do_add_published_version(self, doi, btn):
        # Need the OpenAlex Work to get OA pdf URLs.
        import urllib.parse as _up
        url = ("https://api.openalex.org/works/doi:"
               + _up.quote(doi, safe="")
               + "?mailto=" + _up.quote(metrics.OPENALEX_MAILTO))
        data = metrics._http_get_json(
            url,
            headers={"User-Agent": metrics.OPENALEX_UA,
                     "Accept": "application/json"},
            timeout=15)
        if not data:
            GLib.idle_add(self._add_pv_done, btn, False,
                          "OpenAlex lookup failed")
            return
        # Collect all known OA pdf URLs (best first, then mirrors).
        bol = data.get("best_oa_location") or {}
        pdf_urls = []
        if bol.get("pdf_url"):
            pdf_urls.append(bol["pdf_url"])
        for loc in (data.get("locations") or []):
            if not loc.get("is_oa"):
                continue
            u = loc.get("pdf_url")
            if u and u not in pdf_urls:
                pdf_urls.append(u)
        if not pdf_urls:
            GLib.idle_add(self._add_pv_done, btn, False,
                          "no OA PDF URL available")
            return

        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        # Filename: derive from DOI.
        fname = doi.replace("/", "_") + ".pdf"
        target = os.path.join(LIBRARY_ROOT, fname)
        if os.path.exists(target):
            GLib.idle_add(self._add_pv_done, btn, False, "filename clash")
            return

        # Use the same downloader the author-works dialog does — it
        # already handles atomic write, %PDF- magic-byte check, and
        # the Cloudflare 403 case.
        from . import author_works as _aw
        last_msg = ""
        for i, u in enumerate(pdf_urls):
            if i > 0:
                GLib.idle_add(
                    btn.set_label,
                    "Trying mirror {}/{}…".format(i + 1, len(pdf_urls)))
            ok, msg = _aw._download_pdf(u, target)
            last_msg = msg
            if ok:
                break
        else:
            GLib.idle_add(self._add_pv_done, btn, False, last_msg)
            return

        try:
            rec, status = importer.import_pdf(self.conn, target)
        except Exception as e:
            GLib.idle_add(self._add_pv_done, btn, False, str(e))
            return
        GLib.idle_add(self._add_pv_done, btn, True, status)

    def _add_pv_done(self, btn, ok, status_or_msg):
        if ok:
            btn.set_label("✓ added")
            btn.set_sensitive(False)
            self.status.set_text("Added published version")
            # The watcher's reconcile or our own reload will update the
            # card on next refresh; force one now.
            self._reload(self.search.get_text() or None)
        else:
            btn.set_label("📰 published — Add (failed)")
            btn.set_tooltip_text("Last error: " + str(status_or_msg))
            btn.set_sensitive(True)
        return False

    # --- File-system watcher callbacks --------------------------------

    def _on_watcher_change(self, status):
        """Called on the GLib main thread after the watcher has applied
        a change to the index (import / delete / rename / reconcile /
        sidecar-resync). Debounced so a bulk refresh of N rows produces
        one redraw rather than N."""
        self._pending_reload_status = status
        if getattr(self, "_reload_timer_id", None):
            try:
                GLib.source_remove(self._reload_timer_id)
            except Exception:
                pass
        self._reload_timer_id = GLib.timeout_add(
            300, self._do_debounced_reload)
        return False

    def _do_debounced_reload(self):
        self._reload_timer_id = None
        self._reload(self.search.get_text() or None)
        self.status.set_text("Library updated ({})".format(
            getattr(self, "_pending_reload_status", "")))
        return False  # don't repeat

    def _on_close_request(self, _win):
        # Stop the daemon-friendly bits cleanly so they don't keep
        # writing to the SQLite handle as the window tears down.
        try:
            self._cit_stop.set()
        except Exception:
            pass
        try:
            self.library_watcher.stop()
        except Exception:
            pass
        return False  # let the close proceed

    # --- Authors popover ----------------------------------------------

    def _open_abstract_popover(self, anchor_widget, row):
        """A small popover showing the OpenAlex-reconstructed abstract.
        Header carries the paper's title (so the popover remains
        readable when it's visually detached from the card); body is a
        scrolled, selectable label so users can copy text out.
        Keyboard: Esc dismisses (popover default)."""
        text = row["abstract"] or ""
        title = row["title"] or "(untitled)"

        pop = Gtk.Popover()
        pop.set_parent(anchor_widget)
        pop.set_has_arrow(True)
        pop.set_size_request(560, 380)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        header = Gtk.Label(xalign=0.0)
        header.set_wrap(True)
        header.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        header.set_max_width_chars(70)
        header.set_markup(
            "<small><span alpha='65%'>Abstract  ·  "
            "<i>OpenAlex</i></span></small>\n<b>{}</b>".format(
                safe_pango_markup(title)))
        outer.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER,
                            Gtk.PolicyType.AUTOMATIC)
        body = Gtk.Label(xalign=0.0)
        body.set_wrap(True)
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_max_width_chars(70)
        body.set_selectable(True)
        body.set_text(text)
        body.set_margin_start(2)
        body.set_margin_end(2)
        body.set_margin_top(4)
        body.set_margin_bottom(4)
        scrolled.set_child(body)
        outer.append(scrolled)

        pop.set_child(outer)
        pop.popup()
        # Selectable GtkLabels auto-select-all on focus, so the body
        # arrives pre-selected when the popover opens. Clear it once
        # after the popup; the label stays selectable for on-demand
        # copy-paste.
        GLib.idle_add(lambda: (body.select_region(0, 0), False)[1])

    def _open_cited_by_popover(self, anchor_widget, row):
        """Show two short lists side-by-section in one popover: the
        most recent papers that cite this paper, and the most-cited
        papers that cite this paper. Both come from OpenAlex via
        `cites:` filter queries (one HTTP each)."""
        pop = Gtk.Popover()
        pop.set_parent(anchor_widget)
        pop.set_has_arrow(True)
        pop.set_size_request(620, 540)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        header = Gtk.Label()
        cb = row["citations"] if "citations" in row.keys() else None
        suffix = "  <small alpha='65%'>({} total)</small>".format(cb) if cb else ""
        header.set_markup(
            "<b>Cited by</b>" + suffix +
            "  <span size='small' alpha='65%'>(OpenAlex)</span>")
        header.set_halign(Gtk.Align.START)
        outer.append(header)

        status = Gtk.Label(label="Loading…")
        status.set_halign(Gtk.Align.START)
        outer.append(status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scrolled.set_child(list_box)
        outer.append(scrolled)

        pop.set_child(outer)
        pop.popup()

        doi = row["doi"]

        def _fetch():
            recent = metrics.fetch_cited_by(doi=doi, sort="recent", limit=10)
            cited = metrics.fetch_cited_by(doi=doi, sort="cited", limit=5)
            GLib.idle_add(self._fill_cited_by_popover,
                          status, list_box, recent, cited)

        threading.Thread(target=_fetch, daemon=True).start()

    def _fill_cited_by_popover(self, status, list_box, recent, cited):
        if not recent and not cited:
            status.set_text("No citing papers found.")
            return False
        status.set_visible(False)
        existing = self._existing_dois_set()

        def _section_header(text):
            lbl = Gtk.Label(xalign=0.0)
            lbl.set_markup(
                "<b>{}</b>".format(GLib.markup_escape_text(text)))
            lbl.set_margin_top(4)
            return lbl

        if recent:
            list_box.append(
                _section_header("Most recent ({})".format(len(recent))))
            for w in recent:
                list_box.append(
                    self._build_related_row(
                        w, existing,
                        prefer_date=True, show_citations=True))
        if cited:
            hdr = _section_header("Most cited ({})".format(len(cited)))
            hdr.set_margin_top(12)
            list_box.append(hdr)
            for w in cited:
                list_box.append(
                    self._build_related_row(
                        w, existing,
                        prefer_date=False, show_citations=True))
        return False

    def _open_related_popover(self, anchor_widget, row):
        """Show OpenAlex's related_works for this paper. Fetches in a
        background thread so the UI doesn't freeze."""
        pop = Gtk.Popover()
        pop.set_parent(anchor_widget)
        pop.set_has_arrow(True)
        pop.set_size_request(560, 500)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        header = Gtk.Label()
        header.set_markup(
            "<b>Related works</b>  "
            "<span size='small' alpha='65%'>(OpenAlex similarity)</span>")
        header.set_halign(Gtk.Align.START)
        outer.append(header)

        status = Gtk.Label(label="Loading…")
        status.set_halign(Gtk.Align.START)
        outer.append(status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        scrolled.set_child(list_box)
        outer.append(scrolled)

        pop.set_child(outer)
        pop.popup()

        doi = row["doi"]

        def _fetch():
            rels = metrics.fetch_related_works(doi=doi, limit=12)
            GLib.idle_add(self._fill_related_popover,
                          status, list_box, rels)

        threading.Thread(target=_fetch, daemon=True).start()

    def _fill_related_popover(self, status, list_box, rels):
        if not rels:
            status.set_text("No related works found.")
            return False
        status.set_text("{} works".format(len(rels)))
        existing = self._existing_dois_set()
        for r in rels:
            list_box.append(self._build_related_row(r, existing))
        return False

    def _existing_dois_set(self):
        out = set()
        try:
            cur = self.conn.execute(
                "SELECT doi FROM papers "
                "WHERE doi IS NOT NULL AND doi <> ''")
            for row in cur:
                d = (row[0] or "").lower()
                if d:
                    out.add(d)
        except Exception:
            pass
        return out

    def _build_related_row(self, r, existing_dois,
                           prefer_date=False, show_citations=False):
        """One OpenAlex-result row used by both the Related-works
        and Cited-by popovers: title (bold) on top; first author →
        last author · date · journal · cited Nx underneath; DOI button
        and in-library chip to the right.

        `prefer_date`: when True and `r["publication_date"]` is set,
        show the full date (`2024-09-12`) rather than just the year.
        `show_citations`: when True, append `cited Nx` to the meta
        line if `r["citations"]` > 0."""
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)

        title_lbl = Gtk.Label(xalign=0.0)
        title_lbl.set_wrap(True)
        title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_lbl.set_max_width_chars(70)
        title_lbl.set_markup("<b>{}</b>".format(
            safe_pango_markup(r.get("title") or "(untitled)")))
        info.append(title_lbl)

        meta_bits = []
        fa = r.get("first_author")
        la = r.get("last_author")
        if fa and la and fa != la:
            meta_bits.append("{} → {}".format(fa, la))
        elif fa:
            meta_bits.append(fa)
        if prefer_date and r.get("publication_date"):
            meta_bits.append(r["publication_date"])
        elif r.get("year"):
            meta_bits.append(str(r["year"]))
        if r.get("journal"):
            meta_bits.append(r["journal"])
        if show_citations and r.get("citations"):
            meta_bits.append("cited {}×".format(r["citations"]))
        if meta_bits:
            meta = Gtk.Label(xalign=0.0)
            meta.set_wrap(True)
            meta.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            meta.set_max_width_chars(70)
            meta.set_markup(
                "<small><span alpha='75%'>{}</span></small>".format(
                    GLib.markup_escape_text("  ·  ".join(meta_bits))))
            info.append(meta)
        box.append(info)

        # Right side: DOI button (open in browser) + in-library tag.
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_valign(Gtk.Align.CENTER)
        doi = (r.get("doi") or "").lower()
        if doi and doi in existing_dois:
            in_lib = Gtk.Label()
            in_lib.set_markup(
                '<span foreground="#33aa33" weight="bold">'
                '<small>✓ in library</small></span>')
            in_lib.set_tooltip_text("Already in your library — "
                                    "click to filter")
            in_lib_btn = Gtk.Button()
            in_lib_btn.add_css_class("flat")
            in_lib_btn.set_child(in_lib)
            in_lib_btn.connect(
                "clicked",
                lambda _b, d=doi: self._navigate_to_doi(d))
            right.append(in_lib_btn)
        if r.get("doi"):
            doi_btn = Gtk.Button(label="DOI")
            doi_btn.add_css_class("flat")
            doi_btn.set_tooltip_text("https://doi.org/" + r["doi"])
            doi_btn.connect(
                "clicked",
                lambda _b, d=r["doi"]:
                    open_pdf("https://doi.org/" + d))
            right.append(doi_btn)
        box.append(right)

        frame.set_child(box)
        return frame

    def _open_authors_popover(self, anchor_widget, row):
        """Show a popover anchored to the card's author line, listing
        every author with click-to-filter and (when ORCID known) a
        'find more by this author' button."""
        try:
            authorships = json.loads(row["authorships_json"] or "[]")
        except (TypeError, ValueError):
            authorships = []
        if not authorships:
            try:
                flat = json.loads(row["authors_json"] or "[]")
            except (TypeError, ValueError):
                flat = []
            authorships = [{"name": n} for n in flat]

        pop = Gtk.Popover()
        pop.set_parent(anchor_widget)
        pop.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        title = Gtk.Label()
        title.set_markup("<b>Authors</b>  <small>({})</small>".format(len(authorships)))
        title.set_halign(Gtk.Align.START)
        outer.append(title)

        if not authorships:
            empty = Gtk.Label(label="(no authors)")
            empty.set_halign(Gtk.Align.START)
            outer.append(empty)
        else:
            grid = Gtk.Grid()
            grid.set_column_spacing(8)
            grid.set_row_spacing(2)
            for i, a in enumerate(authorships):
                self._build_author_row(grid, i, a, pop)
            outer.append(grid)

        pop.set_child(outer)
        pop.popup()

    def _build_author_row(self, grid, idx, authorship, popover):
        name = authorship.get("name") or "(unknown)"
        position = (authorship.get("position") or "").lower()
        orcid = authorship.get("orcid")
        institution = authorship.get("institution")

        # Each author occupies two grid rows: the first carries the name
        # button + position label + search button; the second carries
        # the institution underneath. This keeps each author's
        # affiliation visually attached to that author.
        name_row = idx * 2
        inst_row = idx * 2 + 1

        # Filter button: click → set search to the surname, FTS picks up.
        name_btn = Gtk.Button(label=name)
        name_btn.add_css_class("flat")
        name_btn.set_halign(Gtk.Align.START)
        name_btn.set_hexpand(True)
        name_btn.set_tooltip_text("Filter library by this author")
        name_btn.connect("clicked",
                         lambda _b, n=name: self._filter_by_author(n, popover))
        grid.attach(name_btn, 0, name_row, 1, 1)

        # Position marker (subtle): "first" / "last" only.
        if position in ("first", "last"):
            pos_lbl = Gtk.Label()
            pos_lbl.set_markup("<small><i>{}</i></small>".format(position))
            pos_lbl.set_halign(Gtk.Align.START)
            grid.attach(pos_lbl, 1, name_row, 1, 1)

        # ORCID / "more by author" button — only when we have something
        # authoritative to query on (ORCID or OpenAlex ID).
        if orcid or authorship.get("openalex_id"):
            more_btn = Gtk.Button.new_from_icon_name("system-search-symbolic")
            tip = "Find more by this author"
            if orcid:
                tip += "\nORCID: " + orcid
            more_btn.set_tooltip_text(tip)
            more_btn.add_css_class("flat")
            more_btn.connect(
                "clicked",
                lambda _b, a=authorship: self._find_more_by_author(a, popover))
            grid.attach(more_btn, 2, name_row, 1, 1)

        # Institution directly under the name, in small grey text.
        if institution:
            inst_lbl = Gtk.Label()
            inst_lbl.set_markup(
                "<small><span foreground='#888888'>{}</span></small>".format(
                    GLib.markup_escape_text(institution)))
            inst_lbl.set_halign(Gtk.Align.START)
            inst_lbl.set_margin_start(12)
            inst_lbl.set_margin_bottom(2)
            grid.attach(inst_lbl, 0, inst_row, 3, 1)

    def _filter_by_author(self, name, popover):
        # Use the surname (last whitespace-separated token); FTS prefix
        # matching means partial surnames still match.
        parts = (name or "").strip().split()
        query = parts[-1] if parts else (name or "")
        self.search.set_text(query)   # search-changed → _reload
        if popover is not None:
            popover.popdown()

    def _find_more_by_author(self, authorship, popover):
        if popover is not None:
            popover.popdown()
        if not (authorship.get("orcid") or authorship.get("openalex_id")):
            self.status.set_text(
                "No ORCID / OpenAlex ID for {}".format(
                    authorship.get("name") or "this author"))
            return
        author_works.open_window(self, self.conn, authorship)

    # --- Mark labels (user-assigned meanings for the four colours) ---

    _MARK_FALLBACK_NAMES = {
        "red": "Red", "orange": "Orange", "green": "Green", "cyan": "Cyan",
    }

    def _build_mark_filter_dd(self):
        """Build the toolbar's mark-filter dropdown using the current
        self.mark_labels for display strings."""
        items = [("All marks", None)]
        for c in ("red", "orange", "green", "cyan"):
            items.append((
                marks_config.display_for(c, self._MARK_FALLBACK_NAMES[c],
                                         self.mark_labels),
                _MARK_COLORS[c],
            ))
        items.append(("Unmarked", None))
        dd = make_mark_dropdown(items)
        dd.set_tooltip_text("Filter by Mark")
        dd.connect("notify::selected", self._on_mark_filter_changed)
        return dd

    def _refresh_mark_filter_dd(self):
        """Rebuild the toolbar dropdown after labels change."""
        old = self.mark_filter_dd
        selected = old.get_selected()
        # Find old's position so we can re-insert at the same place.
        new_dd = self._build_mark_filter_dd()
        new_dd.set_selected(selected)
        # Replace in the toolbar.
        self._toolbar_box.insert_child_after(new_dd, old)
        self._toolbar_box.remove(old)
        self.mark_filter_dd = new_dd

    def _open_marks_prefs(self, _btn):
        win = Gtk.Window(transient_for=self, modal=True)
        win.set_title("Mark labels")
        win.set_default_size(420, 240)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_start(14)
        outer.set_margin_end(14)
        outer.set_margin_top(14)
        outer.set_margin_bottom(14)

        intro = Gtk.Label()
        intro.set_xalign(0.0)
        intro.set_markup(
            "<small>Give each mark colour a meaning of your choice "
            "(e.g. <i>Must read</i>, <i>My papers</i>, <i>Cool</i>). "
            "Leave blank to use the colour name only.</small>")
        intro.set_wrap(True)
        outer.append(intro)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(10)

        entries = {}
        for i, c in enumerate(("red", "orange", "green", "cyan")):
            chip = Gtk.Label()
            chip.set_markup(
                '<span foreground="{}"><b>●</b></span>  {}'.format(
                    _MARK_COLORS[c], self._MARK_FALLBACK_NAMES[c]))
            chip.set_halign(Gtk.Align.START)
            grid.attach(chip, 0, i, 1, 1)
            e = Gtk.Entry()
            e.set_text(self.mark_labels.get(c, "") or "")
            e.set_hexpand(True)
            grid.attach(e, 1, i, 1, 1)
            entries[c] = e
        outer.append(grid)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btns.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        btns.append(cancel)
        btns.append(save)
        outer.append(btns)

        cancel.connect("clicked", lambda _b: win.close())

        def do_save(_b):
            new_labels = {c: entries[c].get_text().strip()
                          for c in ("red", "orange", "green", "cyan")}
            try:
                marks_config.save(new_labels)
            except Exception as e:
                self.status.set_text("Saving labels failed: " + str(e))
                return
            self.mark_labels = new_labels
            self._refresh_mark_filter_dd()
            self._reload(self.search.get_text() or None)
            win.close()

        save.connect("clicked", do_save)

        win.set_child(outer)
        win.present()


def main(argv):
    conn = index.open_db()
    app = Gtk.Application(application_id="io.github.pemsley.Alexandria")

    def on_activate(app):
        win = BrowserWindow(app, conn)
        win.present()

    app.connect("activate", on_activate)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
