#!/usr/bin/env python3
"""Alexandria — browser for the PDF library and OpenAlex

Reads from the local SQLite index; sidecar JSON files (next to each PDF)
are the source of truth."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib, Gio, GObject, Pango, Adw

from . import (index, edit_dialog, importer, metrics, sidecar, extract,
               viewer, marks_config, prefs, watcher as watcher_mod,
               author_works, bibtex_import, bibtex_export, ris_export,
               csl_export, opener, references_pdf, discover, csl_format,
               feed, feed_window)

LIBRARY_ROOT = prefs.get_library_root()

# Headroom we want to leave on the daily OpenAlex quota for
# foreground actions (your searches, popovers, the citation +
# feed refreshers). When `X-RateLimit-Remaining` drops below
# this, the heaviest background consumer (author-score
# refresher) bows out for the session.
_AUTHOR_SCORE_CREDIT_BUFFER = 1500


# Display flags. Future plan: surface these via a "Display Options"
# popover with Compact / Standard / Verbose presets. For now they are
# module-level constants and default to a quiet card.
display_auto_keywords = False


def open_pdf(path):
    if not opener.open_external(path):
        print("open failed:", path)


from .markup import safe_pango_markup  # noqa: E402,F401  (re-export)


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


def _is_dark_theme(ref_widget):
    """Whether the UI is *actually being rendered* dark.

    Adw.StyleManager.get_dark() alone can't be trusted: it reflects
    the requested libadwaita colour-scheme (portal preference / app
    setting) and is blind to a GTK_THEME=Adwaita:light env override
    (which forces the GTK theme but not the style manager — the
    observed `get_dark()==True` while rendering light). The reliable
    signal is the resolved foreground colour of a realized, styled
    widget: dark theme ⇒ light text (high luminance). `ref_widget`
    must be in the window so its colour is theme-resolved."""
    try:
        c = ref_widget.get_color()   # Gdk.RGBA, GTK >= 4.10
        lum = 0.2126 * c.red + 0.7152 * c.green + 0.0722 * c.blue
        return lum > 0.5             # light text ⇒ dark background
    except Exception:
        try:
            return Adw.StyleManager.get_default().get_dark()
        except Exception:
            return False


def _title_color(ref_widget):
    """Card-title colour, tinted blue-green and theme-aware. Light
    enough on a dark background, dark enough on a light one, tinted
    so the title is visibly not plain black/white."""
    return "#c6e8ef" if _is_dark_theme(ref_widget) else "#2c4750"


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


def make_crossmark_chip(label, severity, year=None, target_doi=None):
    """A small chip flagging publisher-deposited Crossmark updates
    (Retracted / Concern / Correction / etc.) on a paper. Severity-
    coloured: retractions / withdrawals burn red, concerns / removals
    burn red-orange, corrections amber, addenda / clarifications
    grey. Returns None when label is empty."""
    if not label:
        return None
    sev = severity if isinstance(severity, int) else 9
    if sev <= 0:
        fg = "#cc3333"   # retraction / withdrawal
    elif sev == 1:
        fg = "#cc3333"   # partial retraction
    elif sev == 2:
        fg = "#cc6633"   # concern / removal
    elif sev == 3:
        fg = "#ee8800"   # correction / corrigendum / erratum
    else:
        fg = "#888888"   # clarification / addendum / new edition
    frame = Gtk.Frame()
    frame.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label()
    text = "⚠ " + label if sev <= 2 else label
    lbl.set_markup(
        '<span foreground="{}" weight="bold"><small>{}</small></span>'
        .format(fg, GLib.markup_escape_text(text)))
    lbl.set_margin_start(5)
    lbl.set_margin_end(5)
    lbl.set_margin_top(1)
    lbl.set_margin_bottom(1)
    tt = label
    if year:
        tt = "{} ({})".format(label, year)
    if target_doi:
        tt = "{} — see {}".format(tt, target_doi)
    lbl.set_tooltip_text(tt)
    frame.set_child(lbl)
    return frame


def make_oa_chip(is_oa, oa_status):
    """Open-access badge from OpenAlex's `open_access` block. Shown
    only when we *know* a paper is OA — `is_oa is None` (unknown)
    and `is_oa is False` (paywalled) both render no chip; the
    chip's absence is the "we don't know / not OA" signal.

    Hidden when a license chip will already render (caller's job)
    so we don't stack two redundant OA indicators on the title."""
    if not is_oa:
        return None
    label_map = {
        "gold":    "Gold OA",
        "hybrid":  "Hybrid OA",
        "green":   "Green OA",
        "bronze":  "Bronze OA",
        "diamond": "Diamond OA",
    }
    label = label_map.get((oa_status or "").lower(), "Open Access")
    frame = Gtk.Frame()
    frame.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label()
    lbl.set_markup(
        '<span foreground="#338033" weight="bold"><small>{}</small></span>'
        .format(GLib.markup_escape_text(label)))
    lbl.set_margin_start(5)
    lbl.set_margin_end(5)
    lbl.set_margin_top(1)
    lbl.set_margin_bottom(1)
    lbl.set_tooltip_text(
        "OpenAlex says this paper is open access ({}).".format(
            oa_status or "status unknown"))
    frame.set_child(lbl)
    return frame


def make_license_chip(label, url=None):
    """A small chip showing the paper's license — green for CC family,
    muted for publisher-copyright. Returns None when label is empty.
    Tooltip carries the canonical CrossRef license URL when known."""
    if not label:
        return None
    if label.startswith("CC-BY") or label in ("CC0", "Public Domain"):
        # Permissive licenses — green so the reader's eye is drawn to
        # "this one's freely reusable".
        fg = "#338033"
    else:
        # Publisher-copyright. Muted so it sits in the background;
        # the *absence* of a CC chip is the user-relevant signal.
        fg = "#888888"
    frame = Gtk.Frame()
    frame.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label()
    lbl.set_markup(
        '<span foreground="{}" weight="bold"><small>{}</small></span>'
        .format(fg, GLib.markup_escape_text(label)))
    lbl.set_margin_start(5)
    lbl.set_margin_end(5)
    lbl.set_margin_top(1)
    lbl.set_margin_bottom(1)
    lbl.set_tooltip_text(url or label)
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
        # No emoji prefix — U+1F4F0 (📰) is in the macOS colour-emoji
        # range and crashes the Cairo/CoreText pipeline (see memory:
        # `feedback_no_color_emoji`). Plain text reads fine.
        inner.set_markup(
            '<span foreground="#cc6600" weight="bold"><small>'
            'published — Add</small></span>')
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
        return ('<span foreground="#b8860b" weight="bold">'
                '★★★★★ Citation Classic Double</span>')
    if n >= 400:
        return ('<span foreground="#2e8b2e" weight="bold">'
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
# foreground colour (no signal to communicate). The top tier matches
# the goldenrod foreground used by the Citation-Classic-Double star
# row (`#b8860b`) so a paper at that citation level reads as one
# colour-coordinated unit instead of olive-bars-above-gold-stars.
_SPARKLINE_TIERS = (
    (10, None),                  # < 10  → theme grey
    (20, (0x44, 0xaa, 0xaa)),    # < 20  → muted cyan
    (40, (0x44, 0xaa, 0x44)),    # < 40  → muted green
    (None, (0xb8, 0x86, 0x0b)),  # else  → goldenrod, matches stars
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


def _pdf_comment_count(sidecar_path):
    """Number of highlights in this sidecar that carry a non-empty
    comment. Returns 0 on a missing or unreadable sidecar — surfacing
    a "no comments" state is the same as not having an indicator."""
    try:
        record = sidecar.read(sidecar_path)
    except (OSError, ValueError):
        return 0
    return sum(
        1 for h in (record.get("highlights") or [])
        if (h.get("comment") or "").strip())


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
    comment_count = (0 if is_ghost
                     else _pdf_comment_count(row["sidecar_path"]))
    if comment_count:
        # Yellow count-chip in the top-right of the thumbnail signals
        # "this PDF has commented highlights". The chip text is plain
        # digits — colour emoji rendering can crash CoreText/Cairo on
        # macOS, so we lean on the yellow background (matching the
        # viewer's comment-marker colour) and the tooltip below to
        # carry the meaning. `can-target=False` lets the click pass
        # through to the frame's open-viewer gesture.
        overlay = Gtk.Overlay()
        overlay.set_child(img)
        chip = Gtk.Label()
        # Background matches viewer.py:_HIGHLIGHT_FILL (RGB 1.0,0.95,0.0)
        # so the chip, the highlight, and the in-margin marker all read
        # as one yellow.
        chip.set_markup(
            '<small><span background="#fff200" foreground="#000000">'
            ' {} </span></small>'.format(comment_count))
        chip.set_halign(Gtk.Align.END)
        chip.set_valign(Gtk.Align.START)
        chip.set_margin_top(4)
        chip.set_margin_end(4)
        chip.set_can_target(False)
        overlay.add_overlay(chip)
        frame.set_child(overlay)
    else:
        frame.set_child(img)
    if not is_ghost:
        frame.set_cursor_from_name("pointer")
        tip = "View PDF"
        if comment_count:
            tip += "\n{} comment{}".format(
                comment_count, "" if comment_count == 1 else "s")
        frame.set_tooltip_text(tip)
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
        if row["doi"]:
            get_btn.set_tooltip_text(
                "Get PDF — try downloading an open-access copy via "
                "OpenAlex; on failure, open the DOI in your browser.")
            get_btn.connect(
                "clicked",
                lambda _b, r=row: parent_window._on_get_pdf(r))
        else:
            get_btn.set_sensitive(False)
            get_btn.set_tooltip_text(
                "Get PDF — disabled: no DOI on this entry. "
                "Search for one first, or edit metadata to add it.")
        btn_row.append(get_btn)

        # On no-DOI ghosts, offer a "search OpenAlex for DOI" button.
        if not row["doi"]:
            find_btn = Gtk.Button.new_from_icon_name("system-search-symbolic")
            find_btn.set_tooltip_text(
                "Search OpenAlex for a DOI matching this entry's "
                "title + authors + year.")
            find_btn.connect(
                "clicked",
                lambda _b, r=row: parent_window._on_find_doi(r))
            btn_row.append(find_btn)
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
        refs_btn = Gtk.Button.new_from_icon_name("mail-reply-all-symbolic")
        refs_btn.set_tooltip_text(
            "References — papers this one cites (OpenAlex)\n"
            "Listed in publication order")
        refs_btn.connect(
            "clicked",
            lambda b: parent_window._open_references_popover(b, row))
        btn_row.append(refs_btn)
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
    # Crossmark chip — retraction / correction / etc. Drawn before
    # the license chip so the more critical signal lands closer to
    # the title.
    try:
        cm_label = row["crossmark_label"] if "crossmark_label" in row.keys() else None
        cm_sev   = row["crossmark_severity"] if "crossmark_severity" in row.keys() else None
        cm_year  = row["crossmark_year"] if "crossmark_year" in row.keys() else None
        cm_doi   = row["crossmark_doi"] if "crossmark_doi" in row.keys() else None
    except Exception:
        cm_label = cm_sev = cm_year = cm_doi = None
    cm_chip = make_crossmark_chip(cm_label, cm_sev, cm_year, cm_doi)
    if cm_chip is not None:
        title_row.append(cm_chip)
    title = Gtk.Label()
    title.set_markup("<span foreground='{}'><b>{}</b></span>".format(
        _title_color(parent_window),
        safe_pango_markup(row["title"] or "(untitled)")))
    title.set_halign(Gtk.Align.START)
    title.set_wrap(True)
    title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    title.set_max_width_chars(80)
    title.set_selectable(True)
    title.set_hexpand(True)
    title_row.append(title)

    # License / OA chip sits to the right of the title — it's
    # informational rather than critical, so the title gets the
    # left edge and the chip rides the right margin. The title
    # has hexpand=True above, so anything appended after it gets
    # pushed all the way over.
    try:
        lic_label = row["license_label"] if "license_label" in row.keys() else None
        lic_url   = row["license_url"]   if "license_url"   in row.keys() else None
    except Exception:
        lic_label, lic_url = None, None
    lic_chip = make_license_chip(lic_label, lic_url)
    if lic_chip is not None:
        lic_chip.set_valign(Gtk.Align.START)
        title_row.append(lic_chip)
    else:
        # No CrossRef license — fall back to OpenAlex's is_oa /
        # oa_status. Catches Science / Nature / Cell etc. where
        # CrossRef carries no license but OpenAlex knows the OA
        # status via PubMed Central or institutional deposits.
        try:
            is_oa_v = row["is_oa"] if "is_oa" in row.keys() else None
            oa_st   = row["oa_status"] if "oa_status" in row.keys() else None
        except Exception:
            is_oa_v, oa_st = None, None
        oa_chip = make_oa_chip(is_oa_v, oa_st)
        if oa_chip is not None:
            oa_chip.set_valign(Gtk.Align.START)
            title_row.append(oa_chip)
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
    box.pdforg_pdf_path = row["pdf_path"]

    focus_click = Gtk.GestureClick.new()
    focus_click.set_button(0)
    focus_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    focus_click.connect(
        "pressed",
        lambda *_: parent_window._mark_focus(row["pdf_path"]))
    box.add_controller(focus_click)

    # Right-click → "Cite this paper as…" submenu. One button per
    # vendored CSL style; click → format → clipboard → toast.
    cite_click = Gtk.GestureClick.new()
    cite_click.set_button(3)   # secondary mouse / two-finger trackpad
    cite_click.connect(
        "pressed",
        lambda g, n, x, y: _show_cite_menu(g, x, y, row, parent_window))
    box.add_controller(cite_click)

    return box


def _show_cite_menu(gesture, x, y, row, parent_window):
    """Pop a small "Cite this paper as…" menu next to the click. Each
    button formats the citation in that style and copies it to the
    clipboard. Anchored to the card widget; positioned at the click
    point via `set_pointing_to`."""
    anchor = gesture.get_widget()
    pop = Gtk.Popover()
    pop.set_parent(anchor)
    pop.set_has_arrow(True)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    outer.set_margin_start(8)
    outer.set_margin_end(8)
    outer.set_margin_top(6)
    outer.set_margin_bottom(6)
    header = Gtk.Label(xalign=0.0)
    header.set_markup(
        "<small><span alpha='65%'>Cite this paper as…</span></small>")
    outer.append(header)

    for style in csl_format.list_styles():
        btn = Gtk.Button(label=style["label"])
        btn.add_css_class("flat")
        btn.set_halign(Gtk.Align.START)
        btn.connect(
            "clicked",
            lambda _b, s=style: _do_copy_citation(
                s, row, parent_window, pop))
        outer.append(btn)

    pop.set_child(outer)
    pop.popup()


def _do_copy_citation(style, row, parent_window, pop):
    """Format the citation in `style` and copy it to the clipboard.
    Reads the sidecar fresh so any edits since the card was rendered
    are reflected."""
    try:
        rec = sidecar.read(row["sidecar_path"])
    except Exception as e:
        parent_window._toast(
            "Could not read sidecar: {}".format(e), timeout=6)
        pop.popdown()
        return
    try:
        text = csl_format.format_citation(
            rec, style["key"], mode="bibliography")
    except Exception as e:
        parent_window._toast(
            "Format failed: {}".format(e), timeout=6)
        pop.popdown()
        return
    if not text:
        parent_window._toast("Empty citation — missing fields?", timeout=5)
        pop.popdown()
        return
    clipboard = parent_window.get_clipboard()
    clipboard.set(text)
    parent_window._toast(
        "Copied {} citation".format(style["label"]))
    pop.popdown()


class BrowserWindow(Adw.ApplicationWindow):
    def __init__(self, app, conn):
        super().__init__(application=app)
        self.conn = conn
        self.set_title("Alexandria")
        self.set_default_size(900, 700)

        # ---- Window-scoped Gio actions (driven by menu items) -----
        self._install_actions()

        # ---- Search entry (lives inside a Gtk.SearchBar) ----------
        self.search = Gtk.SearchEntry()
        self.search.set_hexpand(True)
        self.search.set_placeholder_text(
            "Search title / authors / DOI / journal")
        self.search.connect("search-changed", self._on_search)
        self.search_bar = Gtk.SearchBar()
        self.search_bar.connect_entry(self.search)
        self.search_bar.set_child(self.search)
        # Modern GNOME pattern: typing anywhere in the window opens the
        # search bar with the typed character.
        self.search_bar.set_key_capture_widget(self)
        self.search_bar.set_show_close_button(True)

        # ---- Marks (used for the filter dropdown + popover) -------
        self.mark_labels = marks_config.load()
        self._MARK_FILTER_VALUES = [None, "red", "orange", "green", "cyan",
                                    index.MARK_FILTER_NONE]
        self.mark_filter_dd = self._build_mark_filter_dd()

        # ---- Sort (key dropdown + asc/desc toggle) ----------------
        # Session-only state; default added_date DESC keeps newly-
        # imported papers at row 0 (the import-flow ergonomic).
        self._SORT_KEY_VALUES = [
            ("added_date",   "Added"),
            ("year",         "Year"),
            ("title",        "Title"),
            ("first_author", "First author"),
            ("last_author",  "Last author"),
            ("citations",    "Citations"),
            ("mark",         "Mark"),
        ]
        self.sort_key_dd = self._build_sort_key_dd()
        self.sort_dir_btn = self._build_sort_dir_btn()

        # ---- HeaderBar --------------------------------------------
        header = Adw.HeaderBar()

        # START: a single "+ Import" menu button covering all import
        # paths and an Export entry. Saves three header slots.
        import_menu = Gio.Menu()
        import_section = Gio.Menu()
        import_section.append("Import Files…",   "win.import-files")
        import_section.append("Import Folder…",  "win.import-folder")
        import_section.append("Import BibTeX…",  "win.import-bibtex")
        import_menu.append_section(None, import_section)
        export_section = Gio.Menu()
        export_section.append("Export BibTeX…",  "win.export-bibtex")
        export_section.append("Export RIS…",     "win.export-ris")
        export_section.append("Export CSL JSON…", "win.export-csl-json")
        import_menu.append_section(None, export_section)
        import_btn = Gtk.MenuButton()
        import_btn.set_label("Import")
        import_btn.set_icon_name("list-add-symbolic")
        import_btn.set_always_show_arrow(True)
        import_btn.set_menu_model(import_menu)
        import_btn.set_tooltip_text("Import / Export")
        header.pack_start(import_btn)

        # END: hamburger first → far right; then mark filter; then
        # search toggle. Order in pack_end is rightmost-first.
        hamburger_menu = Gio.Menu()
        discover_section = Gio.Menu()
        discover_section.append("Discover (OpenAlex)…", "win.discover")
        discover_section.append("Subscriptions…", "win.subscriptions")
        hamburger_menu.append_section(None, discover_section)
        hamburger_menu.append("Preferences…", "win.preferences")
        hamburger_btn = Gtk.MenuButton()
        hamburger_btn.set_icon_name("open-menu-symbolic")
        hamburger_btn.set_menu_model(hamburger_menu)
        hamburger_btn.set_tooltip_text("Main menu")
        header.pack_end(hamburger_btn)

        header.pack_end(self.mark_filter_dd)

        # Sort dir button is packed first (rightmost of the pair),
        # then key dropdown — so the pair reads "key | dir" L→R.
        header.pack_end(self.sort_dir_btn)
        header.pack_end(self.sort_key_dd)

        self.search_toggle = Gtk.ToggleButton()
        self.search_toggle.set_icon_name("system-search-symbolic")
        self.search_toggle.set_tooltip_text("Search (Ctrl-F)")
        self.search_toggle.bind_property(
            "active",
            self.search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL
            | GObject.BindingFlags.SYNC_CREATE)
        header.pack_end(self.search_toggle)

        # ---- Progress strip (toolbar_view top bar) ----------------
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.progress_box.set_margin_start(8)
        self.progress_box.set_margin_end(8)
        self.progress_box.set_margin_bottom(4)
        self.progress_label = Gtk.Label(xalign=0.0)
        self.progress_label.set_hexpand(True)
        self.progress_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_valign(Gtk.Align.CENTER)
        self.progress_box.append(self.progress_label)
        self.progress_box.append(self.progress_bar)
        self.progress_box.set_visible(False)
        self._import_busy = False

        # ---- Status bar (toolbar_view bottom bar) -----------------
        # Stays for now; Phase 3 will replace with Adw.Toast.
        self.status = Gtk.Label()
        self.status.set_halign(Gtk.Align.START)
        self.status.set_use_markup(True)
        self.status.connect("activate-link", self._on_status_link)
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_bar.set_margin_start(8)
        status_bar.set_margin_end(8)
        status_bar.set_margin_top(2)
        status_bar.set_margin_bottom(2)
        status_bar.append(self.status)

        # ---- Results list -----------------------------------------
        self.results_scrolled = Gtk.ScrolledWindow()
        self.results_scrolled.set_vexpand(True)
        self.results_scrolled.set_hexpand(True)
        self.results_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC,
                                         Gtk.PolicyType.AUTOMATIC)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.results_scrolled.set_child(self.results)

        # ---- Toast overlay wraps the results area -----------------
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.results_scrolled)

        # ---- Compose with Adw.ToolbarView -------------------------
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.add_top_bar(self.search_bar)
        toolbar_view.add_top_bar(self.progress_box)
        toolbar_view.add_bottom_bar(status_bar)
        toolbar_view.set_content(self.toast_overlay)

        self.set_content(toolbar_view)

        # Last card the user interacted with (clicked thumbnail, button
        # row, opened a popover…). Reloads scroll this card back into
        # view so filter changes don't lose the user's place.
        self._focus_pdf_path = None

        self._reload(None)

        # Toast a warning if the SQLite DB landed on a network
        # filesystem. WAL files don't survive NFS-style locking
        # quirks intact and the cache is regeneratable, so the right
        # call is to nudge the user to set XDG_STATE_HOME somewhere
        # local. Defer with idle_add so the toast appears after the
        # window has finished mapping.
        if index.is_network_filesystem(index.DEFAULT_DB_PATH):
            GLib.idle_add(self._toast_network_db)

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

        # Background citing-impact pre-fetch. Same shape as the
        # citation refresher: a single daemon thread that walks
        # distinct OpenAlex author IDs across the library and
        # pre-fills the `author_scores` cache so the author
        # dialog's chip resolves instantly. Pause-aware so an
        # arbitrarily large library doesn't burn the polite pool.
        self._asc_stop = threading.Event()
        self._asc_failed_session = set()
        threading.Thread(target=self._author_score_refresher,
                         daemon=True).start()

        # Subscription feed refresher. Walks `subscriptions`,
        # refreshes the stale ones, writes hits into `discovered`.
        # Daemon thread + `_feed_stop` event for clean shutdown,
        # consistent with the other two refreshers above.
        self._feed_stop = threading.Event()
        threading.Thread(target=self._feed_refresher,
                         daemon=True).start()

        # One-shot CrossRef-extras backfill. CrossRef-only, so it
        # doesn't touch the OpenAlex budget; one /works/{doi} call
        # per row fills both the license chip and the Crossmark
        # chip (retraction / correction / etc.) for free. Walks
        # every paper that has a DOI but no cached license; runs
        # independently of the 30-day citation-refresh window.
        self._lic_stop = threading.Event()
        threading.Thread(target=self._crossref_extras_backfill,
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
        dlg.set_message("pdfx not installed")
        dlg.set_detail(
            "The 'pdfx' Python module is not importable in this "
            "environment.\n\n"
            "Metadata extraction will be compromised — titles, authors, "
            "DOI and journal will be sourced only from the PDF's basic "
            "/Info dictionary (often empty), with CrossRef enrichment "
            "as a fallback.\n\n"
            "To fix: pip install pdfx")
        dlg.set_buttons(["OK"])
        dlg.set_default_button(0)
        dlg.show(self)
        return False

    def _focus_search(self, *_args):
        # Reveal the search bar (if hidden) and put the cursor in it.
        self.search_bar.set_search_mode(True)
        self.search.grab_focus()
        self.search.select_region(0, -1)
        return True

    def _on_search(self, entry):
        self._reload(entry.get_text() or None)

    def _install_actions(self):
        """Wire window-scoped Gio actions for menu items. Each action
        delegates to the existing button-style handler (which takes a
        button arg we ignore)."""
        for name, handler in (
            ("import-files",  self._on_import_files),
            ("import-folder", self._on_import_folder),
            ("import-bibtex", self._on_import_bibtex),
            ("export-bibtex", self._on_export_bibtex),
            ("export-ris",    self._on_export_ris),
            ("export-csl-json", self._on_export_csl_json),
            ("discover",      self._open_discover),
            ("subscriptions", self._open_subscriptions),
            ("preferences",   self._open_preferences),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect(
                "activate",
                lambda a, p, h=handler: h(None))
            self.add_action(action)

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
            self._toast("BibTeX import failed (see terminal)", timeout=6)
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
        sort_key, sort_direction = self._current_sort()
        rows = index.search(self.conn, query, mark_filter=mark_filter,
                            sort_key=sort_key, sort_direction=sort_direction)

        try:
            written, skipped = bibtex_export.export_rows_to_file(rows, path)
        except Exception as e:
            print("BibTeX export failed:", e)
            self._toast("Export failed: {}".format(e), timeout=6)
            return

        msg = "Exported {} entries to {}".format(
            written, os.path.basename(path))
        if skipped:
            msg += " ({} skipped — sidecar missing)".format(skipped)
        self._toast(msg)

    # --- Export RIS (mirror of BibTeX export) ---------------------------

    def _on_export_ris(self, _btn):
        dlg = Gtk.FileDialog()
        dlg.set_title("Export RIS")
        dlg.set_initial_name("alexandria-export.ris")
        ris_filter = Gtk.FileFilter()
        ris_filter.set_name("RIS (*.ris)")
        ris_filter.add_pattern("*.ris")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(ris_filter)
        dlg.set_filters(filters)
        dlg.set_default_filter(ris_filter)
        dlg.save(self, None, self._on_ris_save_chosen)

    def _on_ris_save_chosen(self, dlg, result):
        try:
            f = dlg.save_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        if not path.lower().endswith(".ris"):
            path += ".ris"

        # Same as BibTeX: export the *currently visible rows* — the
        # user's search + mark + sort filters are honoured.
        query = self.search.get_text() or None
        mark_filter = self._MARK_FILTER_VALUES[
            self.mark_filter_dd.get_selected()]
        sort_key, sort_direction = self._current_sort()
        rows = index.search(self.conn, query, mark_filter=mark_filter,
                            sort_key=sort_key, sort_direction=sort_direction)

        try:
            written, skipped = ris_export.export_rows_to_file(rows, path)
        except Exception as e:
            print("RIS export failed:", e)
            self._toast("Export failed: {}".format(e), timeout=6)
            return

        msg = "Exported {} entries to {}".format(
            written, os.path.basename(path))
        if skipped:
            msg += " ({} skipped — sidecar missing)".format(skipped)
        self._toast(msg)

    # --- Export CSL JSON (mirror of BibTeX/RIS exports) -----------------

    def _on_export_csl_json(self, _btn):
        dlg = Gtk.FileDialog()
        dlg.set_title("Export CSL JSON")
        dlg.set_initial_name("alexandria-export.json")
        csl_filter = Gtk.FileFilter()
        csl_filter.set_name("CSL JSON (*.json)")
        csl_filter.add_pattern("*.json")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(csl_filter)
        dlg.set_filters(filters)
        dlg.set_default_filter(csl_filter)
        dlg.save(self, None, self._on_csl_save_chosen)

    def _on_csl_save_chosen(self, dlg, result):
        try:
            f = dlg.save_finish(result)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path()
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"

        query = self.search.get_text() or None
        mark_filter = self._MARK_FILTER_VALUES[
            self.mark_filter_dd.get_selected()]
        sort_key, sort_direction = self._current_sort()
        rows = index.search(self.conn, query, mark_filter=mark_filter,
                            sort_key=sort_key, sort_direction=sort_direction)

        try:
            written, skipped = csl_export.export_rows_to_file(rows, path)
        except Exception as e:
            print("CSL JSON export failed:", e)
            self._toast("Export failed: {}".format(e), timeout=6)
            return

        msg = "Exported {} entries to {}".format(
            written, os.path.basename(path))
        if skipped:
            msg += " ({} skipped — sidecar missing)".format(skipped)
        self._toast(msg)

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
        # Stage each picked PDF into the library root before indexing.
        # This makes Import a *copy* operation: the user's source file
        # stays untouched and the sidecar JSON / thumbnail are written
        # alongside the in-library copy. Required inside Flatpak (the
        # FileChooser portal hands us a transient bind-mount that
        # disappears once the app stops referencing it) and a
        # quality-of-life win outside it (no scattered .alexandria
        # files on the user's Desktop / Downloads).
        library_root = prefs.get_library_root()
        n = len(paths)
        for i, p in enumerate(paths, 1):
            try:
                staged, stage_status = importer.stage_into_library(
                    p, library_root, conn=self.conn)
            except Exception as e:
                print("stage failed for {}: {}".format(p, e))
                GLib.idle_add(self._update_progress, i, n, p, None, "error")
                continue
            if stage_status == "duplicate":
                # SHA hit against an already-indexed file. Skip the
                # full import; just report what we matched against so
                # the progress line says "DUP of <existing.pdf>".
                try:
                    rec = sidecar.read(sidecar.sidecar_path_for(staged))
                except Exception:
                    rec = {"pdf_path": staged}
                rec.setdefault("pdf_path", staged)
                GLib.idle_add(self._update_progress, i, n, p, rec, "duplicate")
                continue
            try:
                rec, status = importer.import_pdf(self.conn, staged)
            except Exception as e:
                print("import failed for {}: {}".format(staged, e))
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
            self._toast("Delete failed: {}".format(e), timeout=6)
            return
        self._toast("Deleted: " + os.path.basename(row["pdf_path"]))
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
            self._toast("Renamed to " + new_basename)
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
            # If OpenAlex tripped its session breaker (daily quota
            # exhausted), bail — the rest of this run would just
            # log "OpenAlex rate-limited" per row.
            if metrics.openalex_paused_until() > 0:
                print("[citations] OpenAlex paused, stopping refresher")
                return
            if row["pdf_path"] in self._cit_failed_session:
                continue
            doi = row.get("doi")
            if not doi:
                continue
            (n, src, kw, abstract, authorships, cby,
             oa_title, oa_year, is_oa, oa_status) = metrics.fetch_metrics(doi)
            if n is None:
                self._cit_failed_session.add(row["pdf_path"])
            else:
                today = metrics.today_iso()
                try:
                    rec = sidecar.read(row["sidecar_path"])
                    if not importer._openalex_record_matches(
                            rec.get("title"), rec.get("year"),
                            oa_title, oa_year):
                        if oa_title or oa_year:
                            print(
                                "[citations] OpenAlex record for {} "
                                "looks corrupted — skipping refresh"
                                .format(doi))
                        continue
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
                    if is_oa is not None:
                        rec["is_oa"] = is_oa
                    if oa_status:
                        rec["oa_status"] = oa_status
                    # One-shot CrossRef-extras fetch (license +
                    # crossmark). Cheap — one polite-pool call —
                    # and piggybacks on the citation-refresh visit
                    # so a paper grows its chips the first time
                    # this refresher reaches it. Skip when both
                    # are already cached. License rarely changes;
                    # crossmark *can* (a retraction issued later
                    # is exactly the case we want to catch), but
                    # the dedicated backfill loop revisits stale
                    # rows on demand via the refresh affordance.
                    if not (rec.get("license") and rec.get("crossmark")):
                        try:
                            extras = metrics.fetch_crossref_extras(doi)
                            if extras:
                                if extras.get("license") and not rec.get("license"):
                                    rec["license"] = extras["license"]
                                if extras.get("crossmark"):
                                    rec["crossmark"] = extras["crossmark"]
                        except Exception as e:
                            print("[citations] CrossRef-extras "
                                  "fetch failed for {}: {}".format(doi, e))
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

    def _crossref_extras_backfill(self, initial_delay_seconds=10.0,
                                   pause_seconds=3.0):
        """One-shot pass that fills `license_*` and `crossmark_*` for
        every paper with a DOI but no cached license. One CrossRef
        call per row covers both, so it doesn't compete with the
        OpenAlex budget. Stops cleanly via `_lic_stop`."""
        if self._lic_stop.wait(initial_delay_seconds):
            return
        try:
            rows = index.rows_missing_crossref_extras(self.conn)
        except Exception as e:
            print("[crossref] index lookup failed:", e)
            return
        if not rows:
            return
        filled = 0
        for row in rows:
            if self._lic_stop.is_set():
                return
            doi = row.get("doi")
            if not doi:
                continue
            try:
                extras = metrics.fetch_crossref_extras(doi)
            except Exception as e:
                print("[crossref] fetch failed for {}: {}".format(doi, e))
                extras = None
            if not extras or not (extras.get("license")
                                  or extras.get("crossmark")):
                if self._lic_stop.wait(pause_seconds):
                    return
                continue
            try:
                rec = sidecar.read(row["sidecar_path"])
                if extras.get("license"):
                    rec["license"] = extras["license"]
                if extras.get("crossmark"):
                    rec["crossmark"] = extras["crossmark"]
                sidecar.write(row["sidecar_path"], rec)
                th = row.get("thumb_path")
                mtime = os.path.getmtime(row["sidecar_path"])
                index.upsert(self.conn, row["pdf_path"],
                             row["sidecar_path"], th, rec, mtime)
                filled += 1
            except Exception as e:
                print("[crossref] sidecar write failed for {}: {}"
                      .format(row.get("pdf_path"), e))
            if self._lic_stop.wait(pause_seconds):
                return
        if filled:
            print("[crossref] backfilled {} row(s)".format(filled))
            # Repaint cards so newly-filled chips appear without
            # the user having to scroll or reload.
            GLib.idle_add(lambda: (self._reload(self.search.get_text() or None),
                                    False)[1])

    def _feed_refresher(self, initial_delay_seconds=20.0,
                        check_interval_seconds=900.0,
                        per_sub_pause_seconds=2.0,
                        prune_every_n_passes=4):
        """Walk subscriptions and refresh the stale ones.

        Wakes every `check_interval_seconds` (15 min default) and
        runs through `index.stale_subscriptions`. Each
        subscription's own `fetch_interval_hours` (or the global
        FEED_FETCH_INTERVAL_HOURS default, 6 h) decides whether it
        needs refreshing — the timer just decides when we *check*.

        Every fourth pass we also prune the `discovered` table so
        old entries don't pile up indefinitely. Daemon thread +
        `_feed_stop` event for shutdown; consistent with the
        citation / author-score refreshers above."""
        if self._feed_stop.wait(initial_delay_seconds):
            return
        pass_n = 0
        while True:
            try:
                stale = index.stale_subscriptions(self.conn)
            except Exception as e:
                print("feed refresher: stale lookup failed:", e)
                stale = []
            for sub in stale:
                if self._feed_stop.is_set():
                    return
                if metrics.openalex_paused_until() > 0:
                    print("[feed] OpenAlex paused, skipping pass")
                    break
                try:
                    fetched, new = feed.refresh_subscription(
                        self.conn, sub)
                    index.mark_subscription_fetched(self.conn, sub["id"])
                    if new:
                        print("[feed] {}: {} new article(s)"
                              .format(sub["name"], new))
                        GLib.idle_add(self._on_feed_updated,
                                      sub["id"], new)
                except Exception as e:
                    print("feed refresher: {} failed: {}"
                          .format(sub.get("name"), e))
                if self._feed_stop.wait(per_sub_pause_seconds):
                    return
            pass_n += 1
            if pass_n % prune_every_n_passes == 0:
                try:
                    index.prune_old_discovered(self.conn)
                except Exception as e:
                    print("feed refresher: prune failed:", e)
            if self._feed_stop.wait(check_interval_seconds):
                return

    def _on_feed_updated(self, subscription_id, n_new):
        """Idle callback fired from the refresher thread when a
        subscription gained new articles. If a `FeedWindow` is
        currently open as a transient child, refresh its list so
        the new entries appear without the user having to close
        and re-open it."""
        try:
            for child in self.list_transient_windows():
                if isinstance(child, feed_window.FeedWindow):
                    child._refresh_feed()
        except AttributeError:
            # GTK4 doesn't expose a transient-children list on
            # Window; we walk our own toplevel app windows
            # instead.
            try:
                app = self.get_application()
                if app is not None:
                    for w in app.get_windows():
                        if isinstance(w, feed_window.FeedWindow):
                            w._refresh_feed()
            except Exception:
                pass
        return False

    def _author_score_refresher(self, initial_delay_seconds=180.0,
                                pause_seconds=60.0,
                                per_call_delay_seconds=0.2):
        """Slowly pre-fill the `author_scores` cache for every
        distinct OpenAlex author across the library, so the
        author-dialog's citing-impact chip resolves from cache
        instead of triggering a ~3-min compute on first open.

        Runs once per browser session. Daemon thread, so closing
        the window is enough to stop it (and `_asc_stop` provides
        a cleaner shutdown path between iterations). Longer
        initial delay than the citation refresher because the
        per-author compute is heavier and we'd rather let
        citation counts settle first."""
        if self._asc_stop.wait(initial_delay_seconds):
            return
        try:
            ids = index.stale_author_score_ids(self.conn)
        except Exception as e:
            print("author-score refresher: index lookup failed:", e)
            return
        if not ids:
            return
        for aid in ids:
            if self._asc_stop.is_set():
                return
            # This refresher is THE heaviest OpenAlex consumer in
            # the app (compute_citing_impact = O(top_n × citers
            # per work) API calls per author). Two gates:
            #
            # 1. Hard breaker — daily quota exhausted, server is
            #    rejecting us. Stop the whole walk for the session.
            # 2. Soft budget — credits remaining below the buffer.
            #    Stop walking so we leave headroom for foreground
            #    actions and the lighter refreshers.
            if metrics.openalex_paused_until() > 0:
                print("[author-score] OpenAlex paused, stopping refresher")
                return
            if metrics.openalex_credits_below(_AUTHOR_SCORE_CREDIT_BUFFER):
                remaining = metrics.openalex_credits_remaining()
                print("[author-score] OpenAlex credits at {}, below buffer "
                      "{} — stopping refresher".format(
                          remaining, _AUTHOR_SCORE_CREDIT_BUFFER))
                return
            if aid in self._asc_failed_session:
                continue
            try:
                # Capped at ~5 req/s within one author's compute
                # (polite_delay=0.2). OpenAlex's polite pool tops
                # out at 10 req/s, and we kept bursting through
                # that on prolific authors — the per-minute Cloud-
                # flare throttle then 429'd us with Retry-After
                # in the hundreds of seconds. Half the rate keeps
                # us under the burst ceiling with headroom for
                # foreground actions running on the same pool.
                result = metrics.compute_citing_impact(
                    aid, exclude_self_cites=True,
                    polite_delay=per_call_delay_seconds)
            except Exception as e:
                print("author-score refresher: compute failed for {}: {}"
                      .format(aid, e))
                self._asc_failed_session.add(aid)
                if self._asc_stop.wait(pause_seconds):
                    return
                continue
            if not result:
                self._asc_failed_session.add(aid)
            else:
                try:
                    index.set_author_score(self.conn, aid, result,
                                           self_excluded=True)
                except Exception as e:
                    print("author-score refresher: cache write failed for "
                          "{}: {}".format(aid, e))
            if self._asc_stop.wait(pause_seconds):
                return

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
            self._toast("Drop: no PDFs found")
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
            self._toast("Ghost-drop: not a PDF")
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
        self._toast(msg or status, timeout=5)
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

    # --- Toasts -------------------------------------------------------

    def _toast(self, message, timeout=3):
        """Show a transient Adw.Toast over the results area."""
        t = Adw.Toast.new(message)
        t.set_timeout(timeout)
        self.toast_overlay.add_toast(t)

    def _toast_network_db(self):
        """Surface a one-shot warning when the SQLite cache lives on
        NFS / SMB / sshfs. Fired from the constructor via
        GLib.idle_add. See docs/design/database-and-nfs.md for why
        this matters — even with the per-host DB filename, a network
        FS is still a bad place for SQLite's WAL."""
        msg = ("Warning: SQLite cache is on a network filesystem. "
               "Set XDG_STATE_HOME to a local-disk path.")
        t = Adw.Toast.new(msg)
        t.set_timeout(10)
        self.toast_overlay.add_toast(t)
        # Mirror to stderr — terminal users see it without waiting
        # for the toast and have a record after dismissal.
        print("Alexandria: " + msg + " (db at {})".format(
            index.DEFAULT_DB_PATH),
              file=sys.stderr)
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
        sort_key, sort_direction = self._current_sort()
        rows = index.search(self.conn, query, mark_filter=mark_filter,
                            sort_key=sort_key, sort_direction=sort_direction)
        on_saved = lambda: self._reload(self.search.get_text() or None)
        for r in rows:
            self.results.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            self.results.append(make_card(r, self, self.conn, on_saved,
                                          mark_labels=self.mark_labels))
        self.status.set_text("{} entries".format(len(rows)))
        if self._focus_pdf_path:
            GLib.idle_add(self._scroll_focus_into_view)

    def _mark_focus(self, pdf_path):
        self._focus_pdf_path = pdf_path

    def _scroll_focus_into_view(self):
        target = self._focus_pdf_path
        if not target:
            return False
        child = self.results.get_first_child()
        while child:
            if getattr(child, "pdforg_pdf_path", None) == target:
                a = child.get_allocation()
                adj = self.results_scrolled.get_vadjustment()
                page = adj.get_page_size()
                upper = adj.get_upper()
                adj.set_value(max(0, min(a.y, max(0, upper - page))))
                return False
            child = child.get_next_sibling()
        return False

    def _on_mark_filter_changed(self, _dd, _pspec):
        self._reload(self.search.get_text() or None)

    # --- Find DOI (no-DOI ghost) --------------------------------------

    def _on_find_doi(self, row):
        """Ghost-card "Search OpenAlex for DOI": run a background
        title+author search; on a hit, write the DOI back to the ghost
        sidecar and reload. Toast either way."""
        try:
            rec = sidecar.read(row["sidecar_path"])
        except Exception as e:
            self._toast("Could not read sidecar: {}".format(e), timeout=6)
            return
        title = rec.get("title")
        if not title:
            self._toast("No title to search on", timeout=5)
            return
        self._toast("Searching OpenAlex for «{}»…".format(title[:50]),
                    timeout=2)
        threading.Thread(
            target=self._do_find_doi,
            args=(row["sidecar_path"], rec),
            daemon=True).start()

    def _do_find_doi(self, sc_path, rec):
        try:
            doi = metrics.find_doi(
                title=rec.get("title"),
                year=rec.get("year"),
                author_names=rec.get("authors") or [],
                journal=rec.get("journal"))
        except Exception as e:
            GLib.idle_add(self._toast,
                          "DOI search failed: {}".format(e), 6)
            return
        GLib.idle_add(self._on_find_doi_done, sc_path, doi)

    def _on_find_doi_done(self, sc_path, doi):
        if not doi:
            self._toast("No DOI match found on OpenAlex", timeout=5)
            return False
        # Write DOI back to the ghost sidecar; refresh the index row.
        try:
            rec = sidecar.read(sc_path)
            rec["doi"] = doi
            sidecar.write(sc_path, rec)
            # Find the ghost's pdf_path so we can re-upsert.
            cur = self.conn.execute(
                "SELECT pdf_path, thumb_path FROM papers "
                "WHERE sidecar_path=?", (sc_path,)).fetchone()
            if cur:
                mtime = os.path.getmtime(sc_path)
                index.upsert(self.conn, cur["pdf_path"], sc_path,
                             cur["thumb_path"], rec, mtime)
        except Exception as e:
            self._toast("Found DOI {} but save failed: {}".format(doi, e),
                        timeout=8)
            return False
        self._toast("Found DOI: {}".format(doi), timeout=5)
        self._reload(self.search.get_text() or None)
        return False

    # --- Preprint → published-version actions -------------------------

    def find_existing_by_doi(self, doi):
        """Public lookup used by the citation popover in viewer.py to
        check whether a resolved reference is already in the library.
        Returns the index row dict, or None when there's no match.
        DOI matching is case-insensitive and tolerant of the usual
        prefixes (`doi:`, `https://doi.org/`)."""
        if not doi:
            return None
        try:
            return index.find_duplicate(self.conn, doi=doi)
        except Exception:
            return None

    def show_paper_in_library(self, pdf_path):
        """Public scroll-and-focus used by the citation popover when
        the resolved reference is already in the library — clicking
        "Show in library" lands the user at the existing card."""
        if not pdf_path:
            return
        # Clear any active filter so a search-restricted results list
        # doesn't hide the target card.
        try:
            self.search.set_text("")
        except Exception:
            pass
        self._mark_focus(pdf_path)
        self._reload(None)
        self.present()

    def add_reference_from_viewer(self, br, also_get_pdf, on_done):
        """Public entry point used by `viewer.py` when the user clicks
        the "Add to library" button on a citation popover. Wraps the
        BibTeX-style import path so the viewer doesn't have to know
        about `conn`, `LIBRARY_ROOT`, or the cards-list refresh.

        `br` is a BibTeX-shaped dict (title/authors/year/journal/doi
        plus a synthesised `bibtex_key`). `also_get_pdf=True` chases
        the OA download via the existing `_on_get_pdf` flow once the
        ghost is in. `on_done(success: bool, message: str)` is
        invoked on the GTK thread when the import settles."""
        try:
            rec, status = bibtex_import.import_record(
                self.conn, br, LIBRARY_ROOT)
        except Exception as e:
            GLib.idle_add(on_done, False,
                          "Import failed: {}".format(e))
            return
        # Refresh the cards list so the new ghost is visible.
        self._reload(self.search.get_text() or None)
        if status == "duplicate":
            GLib.idle_add(on_done, True,
                          "Already in your library.")
            return
        if status == "error" or rec is None:
            GLib.idle_add(on_done, False, "Could not import.")
            return
        if also_get_pdf and rec.get("doi"):
            # Find the freshly-imported row by DOI so we can hand it
            # to the existing OA-download flow. The ghost was just
            # upserted, so this should always succeed.
            row = index.find_duplicate(self.conn, doi=rec["doi"],
                                       exclude_path="")
            if row:
                self._on_get_pdf(row)
                GLib.idle_add(on_done, True,
                              "Added; trying to fetch PDF…")
                return
        GLib.idle_add(on_done, True,
                      "Added as ghost." if status == "ghost"
                      else "Added.")

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

        # Collect every OA pdf_url. OpenAlex's `best_oa_location` and
        # `locations` first (when the lookup succeeded), then fall
        # through to Unpaywall's `oa_locations` for any URL OpenAlex
        # didn't surface (BACKLOG: Unpaywall as a Get PDF fallback).
        pdf_urls = []
        if data:
            bol = data.get("best_oa_location") or {}
            if bol.get("pdf_url"):
                pdf_urls.append(bol["pdf_url"])
            for loc in (data.get("locations") or []):
                if not loc.get("is_oa"):
                    continue
                u = loc.get("pdf_url")
                if u and u not in pdf_urls:
                    pdf_urls.append(u)
        unpw = metrics.fetch_oa_locations(doi)
        if unpw:
            for loc in unpw.get("locations") or []:
                u = loc.get("pdf_url")
                if u and u not in pdf_urls:
                    pdf_urls.append(u)
        if not pdf_urls:
            reason = ("no OA PDF URL known to OpenAlex or Unpaywall"
                      if data else "OpenAlex lookup failed and no "
                      "Unpaywall PDF available")
            GLib.idle_add(self._get_pdf_fallback, doi, reason)
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
        self._toast(
            "Direct download failed ({}) — opened DOI in browser; "
            "save and drag the PDF onto the card".format(error_msg),
            timeout=8)
        return False

    def _get_pdf_done(self, status, msg):
        self._toast(msg or status, timeout=5)
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
        # Collect all known OA pdf URLs. OpenAlex's `best_oa_location`
        # + `locations` first (when the lookup succeeded), Unpaywall
        # second for anything OpenAlex didn't surface.
        pdf_urls = []
        if data:
            bol = data.get("best_oa_location") or {}
            if bol.get("pdf_url"):
                pdf_urls.append(bol["pdf_url"])
            for loc in (data.get("locations") or []):
                if not loc.get("is_oa"):
                    continue
                u = loc.get("pdf_url")
                if u and u not in pdf_urls:
                    pdf_urls.append(u)
        unpw = metrics.fetch_oa_locations(doi)
        if unpw:
            for loc in unpw.get("locations") or []:
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
            self._toast("Added published version")
            # The watcher's reconcile or our own reload will update the
            # card on next refresh; force one now.
            self._reload(self.search.get_text() or None)
        else:
            btn.set_label("published — Add (failed)")
            btn.set_tooltip_text("Last error: " + str(status_or_msg))
            btn.set_sensitive(True)
        return False

    # --- File-system watcher callbacks --------------------------------

    def notify_sidecar_changed(self, status="annotation saved"):
        """Public hook for child windows (the PDF viewer) to request a
        library refresh after they have written a sidecar themselves.

        The library watcher deliberately suppresses our own sidecar
        writes (to avoid a write→event→reload feedback loop), so a
        highlight/comment saved in the viewer would otherwise not
        update the card's comment-count chip until an unrelated
        reload. The viewer calls this on save; it rides the same
        300 ms debounce as watcher changes, so rapid successive
        saves collapse into one redraw. The chip is recomputed from
        the on-disk sidecar in make_card, so no DB write is needed."""
        self._on_watcher_change(status)

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
            self._asc_stop.set()
        except Exception:
            pass
        try:
            self._feed_stop.set()
        except Exception:
            pass
        try:
            self._lic_stop.set()
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

    # --- Popover cache (cited-by / references) ----------------------

    def _write_sidecar_cache(self, sc_path, updates):
        """Read the sidecar, merge `updates` into it, and write it
        back. Watcher events on this path are suppressed for a few
        seconds so the cache write doesn't drive a card-list reload."""
        if not sc_path:
            return
        try:
            rec = sidecar.read(sc_path)
        except Exception:
            return
        rec.update(updates)
        if getattr(self, "watcher", None) is not None:
            try:
                self.watcher.suppress(sc_path, 5)
            except Exception:
                pass
        try:
            sidecar.write(sc_path, rec)
        except Exception as e:
            print("cache write failed:", e)

    def _cache_age_str(self, fetched_iso):
        """Render '(cached, fetched X ago)' for a popover header. Returns
        '' when fetched_iso isn't parseable."""
        if not fetched_iso:
            return ""
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(fetched_iso)
            delta = datetime.now(ts.tzinfo) - ts
            secs = int(delta.total_seconds())
        except Exception:
            return ""
        if secs < 60:
            ago = "just now"
        elif secs < 3600:
            ago = "{}m ago".format(secs // 60)
        elif secs < 86400:
            ago = "{}h ago".format(secs // 3600)
        elif secs < 86400 * 30:
            ago = "{}d ago".format(secs // 86400)
        else:
            ago = ts.date().isoformat()
        return ago

    def _make_popover_header(self, title_markup, on_refresh):
        """Build the (title + refresh button) row used by the cached
        popovers. Returns (box, refresh_btn). `on_refresh` is the
        callback invoked when the refresh button is clicked."""
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label()
        title.set_markup(title_markup)
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        hbox.append(title)
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh from OpenAlex")
        refresh_btn.add_css_class("flat")
        refresh_btn.connect("clicked", lambda _b: on_refresh())
        hbox.append(refresh_btn)
        return hbox, refresh_btn

    def _open_references_popover(self, anchor_widget, row):
        """Show the references *of* this paper (the things it cites).

        Primary source is OpenAlex's `referenced_works` (gives us
        structured metadata: title, authors, year, journal, citation
        count, in-library detection by DOI). When OpenAlex has no
        record of this paper, we fall back to parsing the PDF's
        bibliography directly — fewer fields but always available
        offline. Lists are cached per-paper in the sidecar so the
        popover opens instantly on revisit."""
        pop = Gtk.Popover()
        pop.set_parent(anchor_widget)
        pop.set_has_arrow(True)
        pop.set_size_request(620, 540)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        sc_path = row["sidecar_path"]
        doi = row["doi"]
        pdf_path = row["pdf_path"]
        if sidecar.is_ghost_path(pdf_path) or not os.path.exists(pdf_path):
            pdf_path = None

        state = {"loading": False}

        def _do_refresh():
            if state["loading"]:
                return
            state["loading"] = True
            status.set_text("Refreshing…")
            self._clear_box(list_box)
            threading.Thread(target=lambda: _fetch(force=True),
                             daemon=True).start()

        header, refresh_btn = self._make_popover_header(
            "<b>References</b>", _do_refresh)
        outer.append(header)

        status = Gtk.Label(label="")
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

        # Define _fetch *before* the cache check. The cache-hit path
        # below returns early without spawning the thread, but the
        # refresh button (already wired via _do_refresh closure) needs
        # _fetch in scope to do its job when the user clicks it.
        def _fetch(force=False):
            refs = []
            pdf_refs = []
            refs_source = None
            fetched_iso = None
            try:
                try:
                    refs, refs_source = (
                        metrics.fetch_references(doi=doi, limit=50)
                        or ([], None))
                except Exception as e:
                    print("fetch_references failed:", e)
                    refs = []
                    refs_source = None
                if not refs and pdf_path:
                    try:
                        pdf_refs = references_pdf.parse_bibliography(pdf_path)
                    except Exception:
                        pdf_refs = []
                if refs or pdf_refs:
                    fetched_iso = self._now_iso()
                    self._write_sidecar_cache(sc_path, {
                        "references_cache": {
                            "refs": refs,
                            "refs_pdf": pdf_refs,
                            "source": (refs_source if refs else "pdf"),
                            "fetched": fetched_iso,
                        }
                    })
            finally:
                state["loading"] = False
            GLib.idle_add(self._after_refs_fetch,
                          status, list_box, refresh_btn,
                          refs, pdf_refs, fetched_iso, refs_source)

        # Try cache first.
        try:
            rec = sidecar.read(sc_path)
        except Exception:
            rec = None
        cache = (rec or {}).get("references_cache") if rec else None
        if cache and (cache.get("refs") or cache.get("refs_pdf")):
            self._render_references(
                status, list_box,
                cache.get("refs") or [],
                cache.get("refs_pdf") or [],
                from_cache=True,
                fetched_iso=cache.get("fetched"),
                refs_source=cache.get("source"))
            return

        # No cache — fetch.
        status.set_text("Loading…")
        refresh_btn.set_sensitive(False)
        threading.Thread(target=_fetch, daemon=True).start()

    def _after_refs_fetch(self, status, list_box, refresh_btn,
                          refs, pdf_refs, fetched_iso, refs_source=None):
        refresh_btn.set_sensitive(True)
        self._render_references(status, list_box, refs, pdf_refs,
                                from_cache=False, fetched_iso=fetched_iso,
                                refs_source=refs_source)
        return False

    def _render_references(self, status, list_box, refs, pdf_refs,
                           from_cache=False, fetched_iso=None,
                           refs_source=None):
        suffix = ""
        if from_cache and fetched_iso:
            ago = self._cache_age_str(fetched_iso)
            if ago:
                suffix = " · cached {}".format(ago)
        if refs:
            # `fetch_references` falls back from OpenAlex's
            # `referenced_works` to CrossRef's publisher-deposited
            # `reference` field. Either way the rows render the
            # same; the source label tells the user which one
            # populated the list.
            src_label = {
                "openalex": "OpenAlex",
                "crossref": "CrossRef",
            }.get(refs_source or "", "external metadata")
            status.set_markup(
                "<small><span alpha='75%'>{} references "
                "({}, in publication order{})</span></small>".format(
                    len(refs), src_label, suffix))
            existing = self._existing_dois_set()
            for r in refs:
                list_box.append(
                    self._build_related_row(
                        r, existing,
                        prefer_date=False, show_citations=True))
            return
        if pdf_refs:
            status.set_markup(
                "<small><span alpha='75%'>{} references "
                "(parsed from PDF; OpenAlex and CrossRef had no record{})"
                "</span></small>".format(len(pdf_refs), suffix))
            existing = self._existing_dois_set()
            for r in pdf_refs:
                list_box.append(self._build_pdf_ref_row(r, existing))
            return
        status.set_text("No references found.")

    def _clear_box(self, box):
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _now_iso(self):
        from datetime import datetime
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _build_pdf_ref_row(self, r, existing_dois):
        """Render a bibliography entry parsed straight from the PDF.
        Shape is `{n, text, doi, ?key, ?surname, ?year, ?suffix}`:
        a chip on the left (numbered as `[N]`, or author-year as
        `(Surname, YYYY)`), the entry text wrapped in the middle,
        and a DOI button + in-library marker on the right when a
        DOI was extracted."""
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        # Author-year entries get a `(Sheldrick, 2008)`-shape chip so
        # the user can match it against the citations they see in the
        # body text. Numbered entries keep the `[N]` chip. Width
        # widened for the longer label (numbered chips remain compact
        # within it).
        if r.get("surname") and r.get("year"):
            chip_text = "({}, {}{})".format(
                r["surname"], r["year"], r.get("suffix") or "")
            chip_chars = 18
        else:
            chip_text = "[{}]".format(r["n"])
            chip_chars = 5
        n_lbl = Gtk.Label()
        n_lbl.set_markup(
            "<small><span alpha='65%'>{}</span></small>".format(
                GLib.markup_escape_text(chip_text)))
        n_lbl.set_valign(Gtk.Align.START)
        n_lbl.set_xalign(0.0)
        n_lbl.set_width_chars(chip_chars)
        box.append(n_lbl)

        text_lbl = Gtk.Label(xalign=0.0)
        text_lbl.set_wrap(True)
        text_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text_lbl.set_max_width_chars(70)
        text_lbl.set_hexpand(True)
        text_lbl.set_selectable(True)
        text_lbl.set_text(r.get("text") or "")
        box.append(text_lbl)

        doi = (r.get("doi") or "").lower()
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_valign(Gtk.Align.START)
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
        if doi:
            doi_btn = Gtk.Button(label="DOI")
            doi_btn.add_css_class("flat")
            doi_btn.set_tooltip_text("https://doi.org/" + doi)
            doi_btn.connect(
                "clicked",
                lambda _b, d=doi: open_pdf("https://doi.org/" + d))
            right.append(doi_btn)
        box.append(right)

        frame.set_child(box)
        return frame

    def _open_cited_by_popover(self, anchor_widget, row):
        """Show two short lists in one popover: the most recent papers
        that cite this paper, and the most-cited ones. Both come from
        OpenAlex via `cites:` filter queries (one HTTP each). Cached
        per-paper in the sidecar so revisits don't re-query."""
        pop = Gtk.Popover()
        pop.set_parent(anchor_widget)
        pop.set_has_arrow(True)
        pop.set_size_request(620, 540)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        cb = row["citations"] if "citations" in row.keys() else None
        # <small> doesn't accept alpha in Pango markup; only <span>
        # does. Wrap both attributes with a single <span>.
        suffix = ("  <span size='small' alpha='65%'>({} total)</span>"
                  .format(cb) if cb else "")
        title_markup = ("<b>Cited by</b>" + suffix +
                        "  <span size='small' alpha='65%'>(OpenAlex)</span>")

        sc_path = row["sidecar_path"]
        doi = row["doi"]
        state = {"loading": False}

        def _do_refresh():
            if state["loading"]:
                return
            state["loading"] = True
            status.set_text("Refreshing…")
            self._clear_box(list_box)
            threading.Thread(target=lambda: _fetch(force=True),
                             daemon=True).start()

        header, refresh_btn = self._make_popover_header(
            title_markup, _do_refresh)
        outer.append(header)

        status = Gtk.Label(label="")
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

        # Define _fetch *before* the cache check. The cache-hit path
        # below returns early without spawning the thread, but the
        # refresh button (already wired via _do_refresh closure) needs
        # _fetch in scope to do its job when the user clicks it.
        def _fetch(force=False):
            recent = []
            cited = []
            fetched_iso = None
            try:
                try:
                    recent = metrics.fetch_cited_by(
                        doi=doi, sort="recent", limit=10) or []
                except Exception as e:
                    print("fetch_cited_by(recent) failed:", e)
                try:
                    cited = metrics.fetch_cited_by(
                        doi=doi, sort="cited", limit=5) or []
                except Exception as e:
                    print("fetch_cited_by(cited) failed:", e)
                if recent or cited:
                    fetched_iso = self._now_iso()
                    self._write_sidecar_cache(sc_path, {
                        "cited_by_cache": {
                            "recent": recent,
                            "cited": cited,
                            "fetched": fetched_iso,
                        }
                    })
            finally:
                state["loading"] = False
            GLib.idle_add(self._after_cited_by_fetch,
                          status, list_box, refresh_btn,
                          recent, cited, fetched_iso)

        # Try cache first.
        try:
            rec = sidecar.read(sc_path)
        except Exception:
            rec = None
        cache = (rec or {}).get("cited_by_cache") if rec else None
        if cache and (cache.get("recent") or cache.get("cited")):
            self._render_cited_by(
                status, list_box,
                cache.get("recent") or [],
                cache.get("cited") or [],
                from_cache=True,
                fetched_iso=cache.get("fetched"))
            return

        status.set_text("Loading…")
        refresh_btn.set_sensitive(False)
        threading.Thread(target=_fetch, daemon=True).start()

    def _after_cited_by_fetch(self, status, list_box, refresh_btn,
                              recent, cited, fetched_iso):
        refresh_btn.set_sensitive(True)
        self._render_cited_by(status, list_box, recent, cited,
                              from_cache=False, fetched_iso=fetched_iso)
        return False

    def _render_cited_by(self, status, list_box, recent, cited,
                         from_cache=False, fetched_iso=None):
        if not recent and not cited:
            status.set_text("No citing papers found.")
            return
        if from_cache and fetched_iso:
            ago = self._cache_age_str(fetched_iso)
            if ago:
                status.set_markup(
                    "<small><span alpha='65%'>cached {}</span></small>".format(ago))
            else:
                status.set_visible(False)
        else:
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

        # Right side: in-library tag (when applicable), OA chip
        # (when known to be Open Access), DOI button.
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        right.set_valign(Gtk.Align.CENTER)

        # Open Access chip — only renders when `is_oa` is true. Other
        # callers (cited-by / references popovers) don't currently
        # populate this field, so the chip is invisible there until
        # they do.
        if r.get("is_oa"):
            oa_chip = Gtk.Label()
            oa_chip.set_markup(
                '<span foreground="#2a7a7a" weight="bold">'
                '<small>OA</small></span>')
            oa_chip.set_tooltip_text(
                r.get("oa_url") or "Open Access")
            right.append(oa_chip)

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
            # Author lists can run to tens or even hundreds of names
            # (consortium papers). A popover taller than the screen
            # silently fails to display on macOS Gtk4, so cap the height
            # and let the user scroll inside.
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER,
                                Gtk.PolicyType.AUTOMATIC)
            scroller.set_propagate_natural_height(True)
            scroller.set_propagate_natural_width(True)
            scroller.set_max_content_height(500)
            scroller.set_min_content_width(320)
            scroller.set_child(grid)
            outer.append(scroller)

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

    # --- Sort dropdown + direction toggle ------------------------------

    def _build_sort_key_dd(self):
        sl = Gtk.StringList()
        for _key, label in self._SORT_KEY_VALUES:
            sl.append(label)
        dd = Gtk.DropDown(model=sl)
        # Restore the last-used sort key from the prefs file
        # (default: added_date / index 0). Set BEFORE connect so
        # the restore doesn't fire a spurious notify::selected and
        # trigger a no-op reload.
        stored = prefs.load().get("sort_key")
        stored_idx = 0
        if stored:
            for i, (k, _label) in enumerate(self._SORT_KEY_VALUES):
                if k == stored:
                    stored_idx = i
                    break
        dd.set_selected(stored_idx)
        dd.set_tooltip_text("Sort by")
        dd.connect("notify::selected", self._on_sort_changed)
        return dd

    def _build_sort_dir_btn(self):
        btn = Gtk.ToggleButton()
        # Active = DESC (matches the icon). Restore from prefs;
        # default DESC keeps newly-imported papers at row 0.
        stored = prefs.load().get("sort_direction")
        is_desc = (stored != "ASC")  # treat anything not 'ASC' as DESC
        btn.set_active(is_desc)
        btn.set_icon_name(
            "view-sort-descending-symbolic" if is_desc
            else "view-sort-ascending-symbolic")
        btn.set_tooltip_text(
            "Descending (click for ascending)" if is_desc
            else "Ascending (click for descending)")
        btn.connect("toggled", self._on_sort_dir_toggled)
        return btn

    def _current_sort(self):
        idx = self.sort_key_dd.get_selected()
        if idx < 0 or idx >= len(self._SORT_KEY_VALUES):
            idx = 0
        key = self._SORT_KEY_VALUES[idx][0]
        direction = "DESC" if self.sort_dir_btn.get_active() else "ASC"
        return key, direction

    def _persist_sort_choice(self):
        """Stash the current sort key + direction into the prefs
        file. Best-effort — failure to write doesn't matter for
        this session, the choice is already in-memory."""
        key, direction = self._current_sort()
        try:
            data = prefs.load()
            data["sort_key"] = key
            data["sort_direction"] = direction
            prefs.save(data)
        except Exception as e:
            print("prefs: could not persist sort choice:", e)

    def _on_sort_changed(self, _dd, _pspec):
        self._persist_sort_choice()
        self._reload(self.search.get_text() or None)

    def _on_sort_dir_toggled(self, btn):
        if btn.get_active():
            btn.set_icon_name("view-sort-descending-symbolic")
            btn.set_tooltip_text("Descending (click for ascending)")
        else:
            btn.set_icon_name("view-sort-ascending-symbolic")
            btn.set_tooltip_text("Ascending (click for descending)")
        self._persist_sort_choice()
        self._reload(self.search.get_text() or None)

    def _refresh_mark_filter_dd(self):
        """Rebuild the dropdown after labels change. Lives in the
        HeaderBar's pack_end stack — we swap the widget in place by
        unparenting/re-packing."""
        old = self.mark_filter_dd
        selected = old.get_selected()
        new_dd = self._build_mark_filter_dd()
        new_dd.set_selected(selected)
        parent = old.get_parent()
        if parent is not None and hasattr(parent, "remove"):
            parent.remove(old)
        if parent is not None and isinstance(parent, Adw.HeaderBar):
            parent.pack_end(new_dd)
            # The new widget appears at the rightmost end; we want it
            # to sit where the old one did. The simplest fix: also
            # repack the rightmost siblings so the order is restored.
            # In practice this dropdown sits between the search toggle
            # and the hamburger menu, both of which were packed earlier
            # — so a fresh pack_end places `new_dd` to the LEFT of the
            # already-packed siblings. That visually re-creates the
            # original ordering. (Adw.HeaderBar has no insert-at-index.)
        self.mark_filter_dd = new_dd

    def _open_discover(self, _btn):
        discover.open_window(self, self.conn)

    def _open_subscriptions(self, _btn):
        feed_window.open_window(self, self.conn)

    def _open_preferences(self, _btn):
        dlg = Adw.PreferencesDialog()
        dlg.set_title("Preferences")

        page = Adw.PreferencesPage()
        dlg.add(page)

        # ── Library ──────────────────────────────────────────────────────
        lib_group = Adw.PreferencesGroup()
        lib_group.set_title("Library")
        lib_group.set_description("Where your PDF files are stored")
        page.add(lib_group)

        lib_row = Adw.ActionRow()
        lib_row.set_title("PDF Folder")
        lib_row.set_subtitle(LIBRARY_ROOT)
        lib_row.set_subtitle_selectable(True)
        choose_btn = Gtk.Button(label="Choose…")
        choose_btn.set_valign(Gtk.Align.CENTER)
        choose_btn.add_css_class("flat")
        lib_row.add_suffix(choose_btn)
        lib_row.set_activatable_widget(choose_btn)
        lib_group.add(lib_row)

        def _on_lib_folder_chosen(fd, result):
            try:
                folder = fd.select_folder_finish(result)
            except GLib.Error:
                return
            if folder is None:
                return
            new_path = folder.get_path()
            if not new_path:
                return
            global LIBRARY_ROOT
            LIBRARY_ROOT = new_path
            data = prefs.load()
            data["library_root"] = new_path
            try:
                prefs.save(data)
            except Exception as exc:
                self.status.set_text("Saving preferences failed: " + str(exc))
                return
            lib_row.set_subtitle(new_path)
            os.makedirs(new_path, exist_ok=True)
            try:
                self.library_watcher.stop()
            except Exception:
                pass
            self.library_watcher = watcher_mod.LibraryWatcher(
                self.conn, LIBRARY_ROOT,
                on_change_cb=self._on_watcher_change)
            self.library_watcher.start()
            self._reload(self.search.get_text() or None)

        def _on_choose_lib(_b):
            fd = Gtk.FileDialog()
            fd.set_title("Choose PDF Folder")
            fd.set_initial_folder(Gio.File.new_for_path(LIBRARY_ROOT))
            fd.select_folder(self, None, _on_lib_folder_chosen)

        choose_btn.connect("clicked", _on_choose_lib)

        # ── Mark labels ──────────────────────────────────────────────────
        marks_group = Adw.PreferencesGroup()
        marks_group.set_title("Mark Labels")
        marks_group.set_description(
            "Give each mark colour a meaning, "
            "e.g. “Must Read” or “My papers”. "
            "Leave blank to show the colour name only.")
        page.add(marks_group)

        mark_entries = {}
        for color in ("red", "orange", "green", "cyan"):
            row = Adw.EntryRow()
            row.set_title(self._MARK_FALLBACK_NAMES[color])
            row.set_text(self.mark_labels.get(color, "") or "")
            dot = Gtk.Label()
            dot.set_markup(
                '<span foreground="{}"><b>●</b></span>'.format(
                    _MARK_COLORS[color]))
            row.add_prefix(dot)
            marks_group.add(row)
            mark_entries[color] = row

        # ── Annotations ─────────────────────────────────────────────
        ann_group = Adw.PreferencesGroup()
        ann_group.set_title("Annotations")
        ann_group.set_description(
            "Display name shown on the highlights / comments you "
            "make. Existing comments aren't rewritten — only new "
            "ones use this name. Leave blank to fall back to your "
            "OS username.")
        page.add(ann_group)

        ann_row = Adw.EntryRow()
        ann_row.set_title("Comment author")
        ann_row.set_text((prefs.load().get("comment_author") or ""))
        ann_group.add(ann_row)

        def _on_dialog_closed(_d):
            new_labels = {c: mark_entries[c].get_text().strip()
                          for c in ("red", "orange", "green", "cyan")}
            if new_labels != self.mark_labels:
                try:
                    marks_config.save(new_labels)
                except Exception as exc:
                    self.status.set_text(
                        "Saving mark labels failed: " + str(exc))
                else:
                    self.mark_labels = new_labels
                    self._refresh_mark_filter_dd()
                    self._reload(self.search.get_text() or None)

            new_author = ann_row.get_text().strip()
            data = prefs.load()
            old_author = (data.get("comment_author") or "").strip()
            if new_author != old_author:
                if new_author:
                    data["comment_author"] = new_author
                else:
                    data.pop("comment_author", None)
                try:
                    prefs.save(data)
                except Exception as exc:
                    self.status.set_text(
                        "Saving comment-author preference failed: "
                        + str(exc))

        dlg.connect("closed", _on_dialog_closed)
        dlg.present(self)


def _show_db_error_and_quit(app, err):
    """Friendly replacement for the bare sqlite3 traceback when
    `index.open_db()` fails (most often: a stale process is still
    holding the WAL lock). Shows a Gtk.AlertDialog and quits the
    app cleanly when the user dismisses it."""
    import sqlite3 as _sql
    body = (
        "Alexandria can't open its database at:\n"
        "{}\n\n"
        "(The filename contains a 4-character host hash so each "
        "machine has its own private SQLite cache — see "
        "docs/design/database-and-nfs.md.)\n\n"
        "Another Alexandria process on this host may still be "
        "running, or a previous session didn't shut down cleanly "
        "and is still holding the database lock.\n\n"
        "Try closing other Alexandria windows. If that doesn't help, "
        "run this in a terminal and try again:\n"
        "    pkill -f pdforg-browse\n\n"
        "(SQLite said: {})"
    ).format(index.DEFAULT_DB_PATH, err)
    # Mirror to stderr too — terminal users see it without waiting
    # for the dialog dismissal.
    print("Alexandria: cannot open the library database.\n" + body,
          file=sys.stderr)

    dlg = Gtk.AlertDialog()
    dlg.set_modal(True)
    dlg.set_message("Cannot open the library database")
    dlg.set_detail(body)
    dlg.set_buttons(["Quit"])
    dlg.set_default_button(0)
    dlg.set_cancel_button(0)

    def _on_response(d, result):
        try:
            d.choose_finish(result)
        except GLib.Error:
            pass
        app.quit()

    dlg.choose(None, None, _on_response)


def main(argv):
    # Adw.Application initialises libadwaita (theme + dark/light follow
    # the system) and gives us native HeaderBar / Toast support.
    app = Adw.Application(application_id="io.github.pemsley.Alexandria")

    # Thread the conn through a mutable closure so the `shutdown`
    # handler can close it after the last window is gone. SQLite
    # cleanup matters here: WAL-mode databases need a graceful close
    # to flip the WAL pointer; otherwise the *next* launch sees a
    # dirty WAL and (depending on the OS/filesystem) can fail to
    # acquire its lock — the "disk I/O error on launch" symptom
    # documented in the Watcher BACKLOG entry.
    state = {"conn": None}

    def on_activate(app):
        # Libadwaita owns the dark/light decision via Adw.StyleManager.
        # If the user has the legacy GtkSettings:gtk-application-prefer-
        # dark-theme set (via ~/.config/gtk-4.0/settings.ini), libadwaita
        # warns and refuses to honour it. Reset the legacy property so
        # the warning goes away, then express our preference the
        # supported way.
        gs = Gtk.Settings.get_default()
        if gs is not None:
            gs.reset_property("gtk-application-prefer-dark-theme")
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.PREFER_LIGHT)

        # DB open is deferred until after the Adw.Application is up
        # so that a failure (most often a stale-lock condition) can
        # be presented as a Gtk.AlertDialog instead of dumping a raw
        # sqlite3 traceback to the terminal.
        try:
            conn = index.open_db()
        except sqlite3.OperationalError as e:
            _show_db_error_and_quit(app, str(e))
            return

        # One-shot rename of any legacy `*.pdf.meta.json` sidecars
        # to `*.pdf.alexandria` (pre-v0.1.0 on-disk-format change).
        # Both calls are idempotent — they cost a directory walk
        # and a no-op SQL UPDATE on libraries that have already
        # migrated.
        sidecar.migrate_library_sidecars(LIBRARY_ROOT)
        index.migrate_sidecar_paths(conn)

        state["conn"] = conn
        win = BrowserWindow(app, conn)
        win.present()

    def on_shutdown(_app):
        # Fires after the last window is destroyed and the main loop
        # is exiting. Daemon refresher threads are already being
        # torn down by interpreter shutdown; closing the conn here
        # gives SQLite the chance to checkpoint the WAL even if a
        # thread was mid-write when killed.
        conn = state.get("conn")
        if conn is None:
            return
        try:
            # A WAL checkpoint before close pushes all committed
            # pages into the main DB so the WAL can be safely
            # truncated. Without this, a crash or kill mid-write
            # would leave the WAL in a "needs recovery" state that
            # the next open has to detect and fix.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.close()
        except Exception as e:
            print("shutdown: conn close failed:", e)
        state["conn"] = None

    app.connect("activate", on_activate)
    app.connect("shutdown", on_shutdown)
    return app.run(None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
