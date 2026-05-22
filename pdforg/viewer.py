"""Built-in PDF viewer using Poppler + GTK4.

v2: continuous scroll, find-in-page, drag-to-select with highlight +
comment annotations. Annotations live in the sidecar JSON (see
sidecar.new_record's `highlights` field); the PDF file itself is
never modified.
"""

import datetime
import os
import re
import threading
import uuid

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gtk, Gdk, Gio, GLib, Pango, Poppler

from . import (sidecar as sidecar_mod, identity, pdf_links,
               references_pdf, metrics)


_ZOOM_STEP = 1.25
_ZOOM_MIN = 0.25
_ZOOM_MAX = 8.0
_PAGE_SPACING = 8     # pixels between pages in continuous scroll


_HIGHLIGHT_FILL = (1.0, 0.95, 0.0, 0.35)   # yellow, translucent
_HIGHLIGHT_FILL_HOVER = (1.0, 0.80, 0.0, 0.45)
# Margin comment-note marker: same RGB as the highlight, but solid
# so it reads clearly against the white page margin (no text under
# it that needs to show through).
_COMMENT_MARKER_FILL = _HIGHLIGHT_FILL[:3] + (0.95,)


def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


# --- Bibliography-entry → BibTeX-shaped record helpers -----------------
# Used by the citation popover when the user clicks "Add to library".
# Vancouver-style entries look like:
#   "Smith J, Jones K (2020) The clever paper. Nature 581:123-130"
# We need a best-effort split into authors / year / title / journal so
# we can hand a sensible record to bibtex_import.import_record.

_YEAR_PAREN_RE = re.compile(r"\((\d{4})\)\s*")
_TITLE_END_RE = re.compile(r"\.\s+(?=[A-Z])")


def _split_entry_text(text):
    """Best-effort (authors_str, year, title, journal) for a Vancouver-
    style bibliography entry. Returns (None, None, None, None) when
    the year pattern is missing — that's our anchor and without it
    the rest is too unreliable to guess at."""
    m = _YEAR_PAREN_RE.search(text or "")
    if not m:
        return None, None, None, None
    year = int(m.group(1))
    authors_str = text[:m.start()].strip().rstrip(",")
    # Cell-style bibliographies write "(2020). Title…" — the period
    # right after the year-paren combines with the leading space and
    # the capital T to satisfy `_TITLE_END_RE` at offset 0, eating
    # the entire title into `journal_etc` and leaving title empty.
    # Strip leading whitespace and periods so the search starts at
    # the title's first real character.
    rest = text[m.end():].lstrip(" \t.").strip()
    m2 = _TITLE_END_RE.search(rest)
    if m2:
        title = rest[:m2.start()].strip()
        journal_etc = rest[m2.end():].strip()
    else:
        title = rest.rstrip(".").strip()
        journal_etc = ""
    title = title.rstrip(".").strip()
    # "Journal Name 12:345-356" — keep the journal name only.
    journal = re.split(r"\s+\d", journal_etc, maxsplit=1)[0].strip()
    return authors_str, year, title, journal


def _author_surnames(authors_str):
    """First word of each comma-separated author becomes the surname.
    "Smith J, van Dijk AA, Anand-Apte B" → ["Smith", "van", "Anand-Apte"].
    Heuristic — good enough to feed `find_doi`'s author-overlap gate.

    Cell-style entries use Vancouver-with-year and connect the last
    two authors with "and": "Aldridge, S., and Teichmann, S.A.".
    Strip leading "and"/"&" connectors and skip comma-chunks that
    are pure initials ("S.", "S.A.", "M.-A.") so we end up with
    ["Aldridge", "Teichmann"] instead of ["Aldridge", "S.", "and",
    "S.A."]."""
    if not authors_str:
        return []
    out = []
    for chunk in authors_str.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        words = chunk.split()
        while words and words[0].lower() in ("and", "&"):
            words = words[1:]
        if not words:
            continue
        first = words[0]
        # Skip pure-initial chunks like "S.", "S.A.", "M.-A.".
        if re.fullmatch(r"[A-Z]\.(?:[\s\-]?[A-Z]\.)*", first):
            continue
        out.append(first)
    return out


def _build_br(entry, resolved):
    """Build a BibTeX-shaped dict for `bibtex_import.import_record`.
    Prefers the OpenAlex-resolved metadata when available; falls back
    to whatever `parse_bibliography` extracted."""
    if resolved:
        first = resolved.get("first_author") or "ref"
        surname = re.sub(r"[^a-z]", "",
                         (first.split()[-1] if first.split() else "ref")
                         .lower()) or "ref"
        title = resolved.get("title") or ""
        first_word = ""
        for w in title.split():
            cleaned = re.sub(r"[^a-zA-Z]", "", w).lower()
            if cleaned:
                first_word = cleaned
                break
        year = resolved.get("year") or "nodate"
        key = "{}{}{}".format(surname, year, first_word)[:48]
        if not key:
            key = "ref" + uuid.uuid4().hex[:8]
        return {
            "bibtex_key": key,
            "bibtex_type": "article",
            "title": resolved.get("title"),
            "authors": resolved.get("authors") or [],
            "year": str(resolved["year"]) if resolved.get("year") else None,
            "journal": resolved.get("journal"),
            "doi": resolved.get("doi"),
            "bibtex_extra": {},
        }
    # Unresolved: synthesise from the parsed entry.
    authors_str, year, title, journal = _split_entry_text(
        entry.get("text") or "")
    return {
        "bibtex_key": "ref" + uuid.uuid4().hex[:8],
        "bibtex_type": "article",
        "title": title or (entry.get("text") or "")[:120] or "Untitled",
        "authors": [a.strip() for a in (authors_str or "").split(",")
                    if a.strip()],
        "year": str(year) if year else None,
        "journal": journal or None,
        "doi": entry.get("doi"),
        "bibtex_extra": {},
    }


def _truncate(s, n):
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


def open_viewer(parent, pdf_path, sidecar_path=None):
    """Public entry point: open `pdf_path` in a new viewer window.

    `sidecar_path` enables the highlight / comment annotation layer
    (saves go to the sidecar). Omit to open in read-only mode."""
    win = PdfViewerWindow(parent, pdf_path, sidecar_path)
    win.present()
    return win


class PdfViewerWindow(Gtk.Window):
    def __init__(self, parent, pdf_path, sidecar_path=None):
        super().__init__(transient_for=parent)
        self.parent_window = parent  # for callbacks (refresh, get-pdf)
        self.pdf_path = pdf_path
        self.sidecar_path = sidecar_path
        self.set_title(os.path.basename(pdf_path))
        self.set_default_size(820, 1000)

        try:
            uri = Gio.File.new_for_path(pdf_path).get_uri()
            self.doc = Poppler.Document.new_from_file(uri, None)
        except Exception as e:
            self._show_error("Could not open PDF: {}".format(e))
            return

        self.n_pages = self.doc.get_n_pages()
        self.current_page = 0
        self.zoom = 1.0
        # Cumulative y-offset of each page's top edge in the stacked layout.
        self.page_y = [0] * self.n_pages
        self.page_h = [0] * self.n_pages
        # Find-in-page state: list of (page_idx, Poppler.Rectangle); index into it.
        self.find_results = []
        self.find_idx = -1
        self.find_query = ""

        # Annotation state. `highlights` is the live list mirrored to
        # the sidecar. Per-page in-progress drag rectangles are kept
        # for visual feedback only.
        self.highlights = []
        self._drag_state = {}      # page_idx -> dict(start_x, start_y, cur_x, cur_y)
        self._highlights_popover = None
        self._load_highlights()

        # Citation/cross-reference link annotations: built once at
        # open time so a click on `[1]` in the body can jump straight
        # to entry [1] in the references, like Preview does.
        self.citation_links = pdf_links.read_citation_links(pdf_path)
        # Path A finds the Link annotations but can only attach a
        # ref_n when the destination name follows a `CR<N>` pattern
        # (Springer / Nature). For publishers that use opaque
        # destination names (Taylor & Francis's `Anchor N`, array
        # destinations with no name at all, …), fall back to
        # geometry: parse the bibliography, then match each Link's
        # destination y-coord against the parsed entries to recover
        # ref_n. Links that don't land on a bibliography entry —
        # figure / section cross-references — keep ref_n=None and
        # silently skip the popover, which is the right behaviour.
        try:
            bib_positions = references_pdf.bibliography_positions(pdf_path)
        except Exception:
            bib_positions = []
        if bib_positions:
            self.citation_links = pdf_links.assign_ref_n_by_position(
                self.citation_links, bib_positions)
        # Path C: text-based hit-testing for author-year style
        # bibliographies (Acta Cryst, IUCr, older crystallography
        # papers in general). Publisher /Link annotations are absent
        # on these, so we walk the body text for `(Surname, YYYY)`
        # / `Surname (YYYY)` patterns and resolve them against the
        # parsed bibliography. Only fires when parse_bibliography
        # came back author-year (entries carry a `key` field); does
        # nothing for numbered bibliographies, where Path A/B
        # handle the work.
        try:
            ay_bib = references_pdf.parse_bibliography(pdf_path)
        except Exception:
            ay_bib = []
        if ay_bib and any(e.get("key") for e in ay_bib):
            try:
                ay_links = references_pdf.find_author_year_citations(
                    pdf_path, ay_bib)
            except Exception:
                ay_links = {}
            for pi, plinks in ay_links.items():
                self.citation_links.setdefault(pi, []).extend(plinks)
        # Per-page "is the cursor currently over a link?" cache, used
        # to avoid re-setting the cursor on every motion event.
        self._cursor_over_link = {}
        # Parsed bibliography keyed by ref_n; populated lazily on the
        # first citation click (parsing is non-trivial and we don't
        # need it unless the user actually exercises a link).
        self._bibliography_by_n = None
        # The currently-open reference popover, if any. Tracked so
        # repeated clicks on different citations close the previous
        # popover instead of stacking.
        self._reference_popover = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # --- Toolbar ---
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tb.set_margin_start(6)
        tb.set_margin_end(6)
        tb.set_margin_top(4)
        tb.set_margin_bottom(4)

        first_btn = Gtk.Button.new_from_icon_name("go-first-symbolic")
        first_btn.set_tooltip_text("First page")
        prev_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        prev_btn.set_tooltip_text("Previous page")
        next_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        next_btn.set_tooltip_text("Next page")
        last_btn = Gtk.Button.new_from_icon_name("go-last-symbolic")
        last_btn.set_tooltip_text("Last page")

        self.page_entry = Gtk.Entry()
        self.page_entry.set_max_length(6)
        self.page_entry.set_max_width_chars(5)
        self.page_entry.set_alignment(0.5)
        self.page_entry.connect("activate", self._on_page_entered)

        self.page_total_lbl = Gtk.Label()

        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        zoom_out_btn = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        zoom_out_btn.set_tooltip_text("Zoom out")
        zoom_reset_btn = Gtk.Button.new_from_icon_name("zoom-original-symbolic")
        zoom_reset_btn.set_tooltip_text("Reset zoom (100%)")
        zoom_in_btn = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        zoom_in_btn.set_tooltip_text("Zoom in")
        zoom_fit_btn = Gtk.Button.new_from_icon_name("zoom-fit-best-symbolic")
        zoom_fit_btn.set_tooltip_text("Fit page width")

        self.zoom_lbl = Gtk.Label()

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.find_entry = Gtk.SearchEntry()
        self.find_entry.set_placeholder_text("Find in PDF")
        self.find_entry.set_hexpand(True)
        self.find_entry.connect("activate", self._on_find_activate)
        self.find_entry.connect("search-changed", self._on_find_changed)
        find_prev_btn = Gtk.Button.new_from_icon_name("go-up-symbolic")
        find_prev_btn.set_tooltip_text("Previous match (Shift-F3)")
        find_prev_btn.connect("clicked", lambda _b: self._find_step(-1))
        find_next_btn = Gtk.Button.new_from_icon_name("go-down-symbolic")
        find_next_btn.set_tooltip_text("Next match (F3)")
        find_next_btn.connect("clicked", lambda _b: self._find_step(+1))
        self.find_count_lbl = Gtk.Label()

        sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        # Highlights-sidebar toggle. Only meaningful when a sidecar is
        # available (otherwise we can't load highlights).
        self.sidebar_toggle = Gtk.ToggleButton()
        self.sidebar_toggle.set_icon_name("view-list-symbolic")
        self.sidebar_toggle.set_tooltip_text("Show highlights")
        self.sidebar_toggle.connect("toggled", self._on_sidebar_toggled)
        if not self.sidecar_path:
            self.sidebar_toggle.set_sensitive(False)

        # Reference popover button. Hidden until the user clicks an
        # in-text citation; from then on it shows "Reference [N]" and
        # toggles a popover with the resolved metadata + actions.
        # Anchoring the popover to a stable toolbar button (instead
        # of the scrollable page widget) sidesteps a focus / parent-
        # reallocation race that was dismissing the popover ~1s after
        # opening, and matches how browse.py's References / Cited-by
        # popovers are built.
        self.ref_btn = Gtk.MenuButton()
        self.ref_btn.set_label("Reference")
        self.ref_btn.set_tooltip_text(
            "Reference details for the citation you last clicked. "
            "Click to toggle.")
        self.ref_btn.set_visible(False)

        for w in (first_btn, prev_btn, self.page_entry, self.page_total_lbl,
                  next_btn, last_btn, sep,
                  zoom_out_btn, zoom_reset_btn, zoom_in_btn, zoom_fit_btn,
                  self.zoom_lbl, sep2,
                  self.find_entry, find_prev_btn, find_next_btn,
                  self.find_count_lbl, sep3, self.sidebar_toggle,
                  self.ref_btn):
            tb.append(w)

        outer.append(tb)

        # --- Stacked pages inside a scrolled window ---
        self.pages_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=_PAGE_SPACING)
        self.pages_box.set_halign(Gtk.Align.CENTER)

        self.page_widgets = []
        for i in range(self.n_pages):
            da = Gtk.DrawingArea()
            da.set_draw_func(self._draw_one_page, i)
            self.page_widgets.append(da)
            self.pages_box.append(da)
            # Click is always wired: citation-link jumps don't need a
            # sidecar, only the highlight-creation drag does.
            self._attach_click_controller(da, i)
            self._attach_link_motion_controller(da, i)
            if self.sidecar_path:
                self._attach_annotation_controllers(da, i)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        self.scrolled.set_hexpand(True)
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC,
                                 Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_child(self.pages_box)
        outer.append(self.scrolled)
        self.set_child(outer)

        # --- Wire up actions ---
        first_btn.connect("clicked", lambda _b: self._goto(0))
        prev_btn.connect("clicked", lambda _b: self._goto(self.current_page - 1))
        next_btn.connect("clicked", lambda _b: self._goto(self.current_page + 1))
        last_btn.connect("clicked", lambda _b: self._goto(self.n_pages - 1))
        zoom_out_btn.connect("clicked", lambda _b: self._set_zoom(self.zoom / _ZOOM_STEP))
        zoom_reset_btn.connect("clicked", lambda _b: self._set_zoom(1.0))
        zoom_in_btn.connect("clicked", lambda _b: self._set_zoom(self.zoom * _ZOOM_STEP))
        zoom_fit_btn.connect("clicked", lambda _b: self._fit_width())

        # Update current-page indicator as the user scrolls.
        self.scrolled.get_vadjustment().connect(
            "value-changed", self._on_scroll)

        # Ctrl+scroll → zoom; uncondition scroll falls through to the
        # ScrolledWindow's own handler.
        wheel = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._on_wheel)
        self.scrolled.add_controller(wheel)

        # Keyboard shortcuts.
        sc = Gtk.ShortcutController()
        for trigger, fn in [
            ("Page_Up",        lambda *_: self._goto(self.current_page - 1)),
            ("Page_Down",      lambda *_: self._goto(self.current_page + 1)),
            ("<Control>Home",  lambda *_: self._goto(0)),
            ("<Control>End",   lambda *_: self._goto(self.n_pages - 1)),
            ("plus",           lambda *_: self._set_zoom(self.zoom * _ZOOM_STEP)),
            ("<Control>plus",  lambda *_: self._set_zoom(self.zoom * _ZOOM_STEP)),
            ("minus",          lambda *_: self._set_zoom(self.zoom / _ZOOM_STEP)),
            ("<Control>minus", lambda *_: self._set_zoom(self.zoom / _ZOOM_STEP)),
            ("<Control>0",     lambda *_: self._set_zoom(1.0)),
            ("<Control>f",     lambda *_: self._focus_find()),
            ("F3",             lambda *_: self._find_step(+1)),
            ("<Shift>F3",      lambda *_: self._find_step(-1)),
            ("Escape",         lambda *_: self._clear_find()),
        ]:
            sc.add_shortcut(Gtk.Shortcut.new(
                trigger=Gtk.ShortcutTrigger.parse_string(trigger),
                action=Gtk.CallbackAction.new(
                    lambda *a, _fn=fn: (_fn(), True)[1])))
        self.add_controller(sc)

        self._refresh_sizes()
        self._update_page_indicator()

    # --- Layout / sizing ----------------------------------------------

    def _refresh_sizes(self):
        """Recompute every page's pixel size for the current zoom level
        and cache the cumulative y-offsets used for goto / scroll-to-page."""
        y = 0
        for i, da in enumerate(self.page_widgets):
            page = self.doc.get_page(i)
            pw_pt, ph_pt = page.get_size()
            pw = int(pw_pt * self.zoom)
            ph = int(ph_pt * self.zoom)
            da.set_content_width(pw)
            da.set_content_height(ph)
            da.queue_draw()
            self.page_y[i] = y
            self.page_h[i] = ph
            y += ph + _PAGE_SPACING
        self.page_total_lbl.set_text(" / {}".format(self.n_pages))
        self.zoom_lbl.set_text(" {:.0f}%".format(self.zoom * 100))

    def _draw_one_page(self, _area, cr, _w, _h, page_idx):
        page = self.doc.get_page(page_idx)
        pw, ph = page.get_size()
        cr.scale(self.zoom, self.zoom)
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(0, 0, pw, ph)
        cr.fill()
        page.render(cr)

        # Saved highlights (annotation layer). Stored quads are in PDF
        # points with y-down-from-top, so they render directly under
        # cr.scale(zoom, zoom).
        for h in self.highlights:
            if h.get("page") != page_idx:
                continue
            cr.set_source_rgba(*_HIGHLIGHT_FILL)
            for q in h.get("quads") or []:
                if len(q) == 4:
                    cr.rectangle(q[0], q[1], q[2], q[3])
                    cr.fill()
            # Margin sticky-note marker for highlights with a comment.
            if (h.get("comment") or "").strip():
                # Pick the topmost quad to anchor the icon.
                top_quad = min(h["quads"], key=lambda q: q[1]) if h.get("quads") else None
                if top_quad:
                    self._draw_comment_marker(cr, top_quad, ph, pw)

        # In-progress drag rectangle (visual feedback only).
        ds = self._drag_state.get(page_idx)
        if ds:
            x = min(ds["start_x"], ds["cur_x"])
            y = min(ds["start_y"], ds["cur_y"])
            w = abs(ds["cur_x"] - ds["start_x"])
            h = abs(ds["cur_y"] - ds["start_y"])
            cr.set_source_rgba(0.2, 0.5, 0.9, 0.18)
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(0.2, 0.5, 0.9, 0.55)
            cr.set_line_width(1.0 / self.zoom)
            cr.rectangle(x, y, w, h)
            cr.stroke()

        # Find-in-page highlights. Poppler.find_text returns rectangles
        # in PDF coordinates with y growing upward from the page bottom;
        # convert by reflecting y about the page height.
        for i, (pi, rect) in enumerate(self.find_results):
            if pi != page_idx:
                continue
            x = rect.x1
            y = ph - rect.y2
            w = rect.x2 - rect.x1
            h = rect.y2 - rect.y1
            if i == self.find_idx:
                cr.set_source_rgba(1.0, 0.55, 0.0, 0.55)   # current → orange
            else:
                cr.set_source_rgba(1.0, 0.95, 0.0, 0.40)   # others  → yellow
            cr.rectangle(x, y, w, h)
            cr.fill()

    def _draw_comment_marker(self, cr, quad, ph, pw):
        """Tiny note marker pinned to the right edge of the page,
        next to the highlight. Filled with the same yellow as the
        highlight (via _COMMENT_MARKER_FILL) so the highlight, the
        marker, and the card-level chip all read as one signal."""
        x = pw - 14
        y = max(2, quad[1] - 2)
        cr.set_source_rgba(*_COMMENT_MARKER_FILL)
        cr.rectangle(x, y, 12, 10)
        cr.fill()
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.6)
        cr.set_line_width(0.8)
        cr.rectangle(x, y, 12, 10)
        cr.stroke()

    # --- Navigation ----------------------------------------------------

    def _goto(self, n):
        if n < 0:
            n = 0
        if n >= self.n_pages:
            n = self.n_pages - 1
        self.current_page = n
        # Scroll so this page's top edge is at the viewport's top.
        # GLib.idle_add lets layout settle first if the box hasn't
        # been allocated yet.
        def _do_scroll():
            adj = self.scrolled.get_vadjustment()
            adj.set_value(self.page_y[n])
            return False
        GLib.idle_add(_do_scroll)
        self._update_page_indicator()

    def _on_page_entered(self, entry):
        try:
            n = int(entry.get_text()) - 1
        except ValueError:
            n = self.current_page
        self._goto(n)

    def _on_scroll(self, adj):
        """Update the page indicator based on which page occupies the
        viewport's vertical centre."""
        target = adj.get_value() + adj.get_page_size() / 2
        new_page = self._page_for_y(target)
        if new_page != self.current_page:
            self.current_page = new_page
            self._update_page_indicator()

    def _page_for_y(self, y):
        # Linear scan; n_pages is typically tens, so this is fine.
        for i in range(self.n_pages):
            page_top = self.page_y[i]
            page_bottom = page_top + self.page_h[i]
            if y < page_bottom:
                return i
        return self.n_pages - 1

    def _update_page_indicator(self):
        # Avoid feedback loop with `activate`: only set if different.
        target = str(self.current_page + 1)
        if self.page_entry.get_text() != target:
            self.page_entry.set_text(target)

    # --- Zoom ----------------------------------------------------------

    def _set_zoom(self, z):
        z = max(_ZOOM_MIN, min(z, _ZOOM_MAX))
        if abs(z - self.zoom) < 1e-6:
            return
        # Preserve the document position under the viewport centre across
        # the zoom change.
        adj = self.scrolled.get_vadjustment()
        anchor_y_doc = (adj.get_value() + adj.get_page_size() / 2)
        anchor_page = self._page_for_y(anchor_y_doc)
        # Fraction of the page the anchor sits at.
        frac = ((anchor_y_doc - self.page_y[anchor_page])
                / max(1.0, self.page_h[anchor_page]))

        self.zoom = z
        self._refresh_sizes()

        new_y = (self.page_y[anchor_page]
                 + frac * self.page_h[anchor_page]
                 - adj.get_page_size() / 2)
        GLib.idle_add(lambda: (adj.set_value(max(0, new_y)), False)[1])

    def _fit_width(self):
        page = self.doc.get_page(self.current_page)
        w_pt, _ = page.get_size()
        avail = self.scrolled.get_allocated_width() - 24
        if avail > 0 and w_pt > 0:
            self._set_zoom(avail / w_pt)

    def _on_wheel(self, controller, _dx, dy):
        state = controller.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            if dy < 0:
                self._set_zoom(self.zoom * _ZOOM_STEP)
            elif dy > 0:
                self._set_zoom(self.zoom / _ZOOM_STEP)
            return True   # consumed
        return False      # ScrolledWindow does the actual scrolling

    # --- Highlights index ----------------------------------------------

    def _on_sidebar_toggled(self, btn):
        """Show or hide a popover listing all highlights for this PDF."""
        if not btn.get_active():
            if self._highlights_popover is not None:
                self._highlights_popover.popdown()
            return

        pop = Gtk.Popover()
        pop.set_parent(btn)
        pop.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        header = Gtk.Label()
        header.set_markup("<b>Highlights</b>  <small>({})</small>".format(
            len(self.highlights)))
        header.set_halign(Gtk.Align.START)
        outer.append(header)

        if not self.highlights:
            empty = Gtk.Label(label="(no highlights yet)")
            empty.set_halign(Gtk.Align.START)
            empty.add_css_class("dim-label")
            outer.append(empty)
        else:
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_min_content_width(380)
            scrolled.set_min_content_height(min(420, 40 + 50 * len(self.highlights)))
            scrolled.set_policy(Gtk.PolicyType.NEVER,
                                Gtk.PolicyType.AUTOMATIC)
            list_box = Gtk.ListBox()
            list_box.set_selection_mode(Gtk.SelectionMode.NONE)
            for h in self.highlights:
                list_box.append(self._build_highlight_row(h, pop))
            scrolled.set_child(list_box)
            outer.append(scrolled)

        pop.set_child(outer)
        pop.connect("closed", self._on_highlights_popover_closed)
        self._highlights_popover = pop
        pop.popup()

    def _on_highlights_popover_closed(self, _pop):
        self._highlights_popover = None
        # Sync the toggle so it un-presses when the user clicks outside.
        if self.sidebar_toggle.get_active():
            self.sidebar_toggle.set_active(False)

    def _build_highlight_row(self, h, pop):
        page = h.get("page", 0)
        text = h.get("text") or ""
        comment = h.get("comment") or ""

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(4)
        row.set_margin_end(4)
        row.set_margin_top(4)
        row.set_margin_bottom(4)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)

        snippet = _truncate(text, 120) if text else "(no text captured)"
        head = Gtk.Label()
        head.set_markup(
            "<small><b>p.{}</b></small>  {}".format(
                page + 1, GLib.markup_escape_text(snippet)))
        head.set_xalign(0.0)
        head.set_wrap(True)
        head.set_max_width_chars(60)
        info.append(head)
        if comment:
            cmt = Gtk.Label()
            cmt.set_markup("<small><i>{}</i></small>".format(
                GLib.markup_escape_text(_truncate(comment, 200))))
            cmt.set_xalign(0.0)
            cmt.set_wrap(True)
            cmt.set_max_width_chars(60)
            info.append(cmt)
        row.append(info)

        goto_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
        goto_btn.set_tooltip_text("Jump to highlight")
        goto_btn.add_css_class("flat")
        goto_btn.connect(
            "clicked",
            lambda _b, hh=h: self._scroll_to_highlight(hh, pop))
        row.append(goto_btn)
        return row

    def _scroll_to_highlight(self, h, pop=None):
        page = h.get("page", 0)
        if page < 0 or page >= self.n_pages:
            return
        quads = h.get("quads") or []
        # Use the topmost (smallest y) quad for the scroll target.
        if quads:
            y_pdf = min(q[1] for q in quads if len(q) >= 4)
        else:
            y_pdf = 0
        target_doc_y = self.page_y[page] + y_pdf * self.zoom
        adj = self.scrolled.get_vadjustment()
        new_y = max(0, target_doc_y - adj.get_page_size() / 3)
        GLib.idle_add(lambda: (adj.set_value(new_y), False)[1])
        self.current_page = page
        self._update_page_indicator()
        if pop is not None:
            pop.popdown()

    # --- Find in PDF ---------------------------------------------------

    def _focus_find(self):
        self.find_entry.grab_focus()
        self.find_entry.select_region(0, -1)

    def _on_find_changed(self, entry):
        # Don't trigger a fresh search on every keystroke — wait for
        # Enter via _on_find_activate. Just clear stale results so the
        # highlights disappear while the user is typing a new query.
        if entry.get_text() != self.find_query:
            self._reset_results()

    def _on_find_activate(self, entry):
        query = entry.get_text().strip()
        if not query:
            self._clear_find()
            return
        if query == self.find_query and self.find_results:
            # Same query → step to next.
            self._find_step(+1)
            return
        self._do_search(query)

    def _do_search(self, query):
        self.find_query = query
        results = []
        for i in range(self.n_pages):
            try:
                rects = self.doc.get_page(i).find_text(query) or []
            except Exception:
                rects = []
            for r in rects:
                results.append((i, r))
        self.find_results = results
        self.find_idx = 0 if results else -1
        self._update_find_count()
        if self.find_idx >= 0:
            self._scroll_to_current_match()
        self._redraw_all()

    def _find_step(self, delta):
        if not self.find_results:
            # No active search; treat F3 as "search whatever's in the entry"
            text = self.find_entry.get_text().strip()
            if text:
                self._do_search(text)
            return
        n = len(self.find_results)
        self.find_idx = (self.find_idx + delta) % n
        self._update_find_count()
        self._scroll_to_current_match()
        self._redraw_all()

    def _scroll_to_current_match(self):
        if self.find_idx < 0 or self.find_idx >= len(self.find_results):
            return
        page_idx, rect = self.find_results[self.find_idx]
        page = self.doc.get_page(page_idx)
        _, ph = page.get_size()
        # Top of result in widget pixels (y_pdf measured from page bottom).
        y_in_page = (ph - rect.y2) * self.zoom
        target = self.page_y[page_idx] + y_in_page
        adj = self.scrolled.get_vadjustment()
        # Centre-ish: aim for one third from the top so context is visible.
        new_y = target - adj.get_page_size() / 3
        GLib.idle_add(lambda: (adj.set_value(max(0, new_y)), False)[1])
        self.current_page = page_idx
        self._update_page_indicator()

    def _update_find_count(self):
        if not self.find_results:
            if self.find_query:
                self.find_count_lbl.set_text("0 matches")
            else:
                self.find_count_lbl.set_text("")
            return
        self.find_count_lbl.set_text("{} of {}".format(
            self.find_idx + 1, len(self.find_results)))

    def _reset_results(self):
        self.find_results = []
        self.find_idx = -1
        self.find_query = ""
        self._update_find_count()
        self._redraw_all()

    def _clear_find(self):
        self.find_entry.set_text("")
        self._reset_results()

    def _redraw_all(self):
        for w in self.page_widgets:
            w.queue_draw()

    # --- Annotation layer ---------------------------------------------

    def _load_highlights(self):
        if not self.sidecar_path:
            return
        try:
            rec = sidecar_mod.read(self.sidecar_path)
        except Exception:
            return
        self.highlights = list(rec.get("highlights") or [])

    def _save_highlights(self):
        if not self.sidecar_path:
            return
        try:
            rec = sidecar_mod.read(self.sidecar_path)
        except Exception:
            # No sidecar yet — bail rather than constructing a half-record.
            return
        rec["highlights"] = self.highlights
        try:
            sidecar_mod.write(self.sidecar_path, rec)
        except Exception as e:
            print("viewer: sidecar write failed:", e)
            return
        # Tell the library window to refresh — the watcher
        # suppresses our own sidecar writes, so the comment-count
        # chip wouldn't update otherwise. Debounced on the browser
        # side, so save-spamming is fine.
        pw = self.parent_window
        if pw is not None and hasattr(pw, "notify_sidecar_changed"):
            try:
                pw.notify_sidecar_changed("annotation saved")
            except Exception:
                pass

    def _attach_click_controller(self, da, page_idx):
        """Per-page primary-button click. Handles citation-link jumps
        (always available) and existing-highlight popovers (when a
        sidecar is loaded)."""
        click = Gtk.GestureClick.new()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect(
            "released",
            lambda g, n, x, y, _i=page_idx: self._on_page_click(g, n, x, y, _i))
        da.add_controller(click)

    def _attach_annotation_controllers(self, da, page_idx):
        """Per-page drag — selection → highlight popover. Click is
        wired separately by `_attach_click_controller` because
        citation links should work even when no sidecar is loaded."""
        drag = Gtk.GestureDrag.new()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin",
                     lambda g, sx, sy, _i=page_idx: self._on_drag_begin(g, sx, sy, _i))
        drag.connect("drag-update",
                     lambda g, dx, dy, _i=page_idx: self._on_drag_update(g, dx, dy, _i))
        drag.connect("drag-end",
                     lambda g, dx, dy, _i=page_idx: self._on_drag_end(g, dx, dy, _i))
        da.add_controller(drag)

    # Coord helpers: widget pixels → PDF points (y-down-from-top).
    def _to_pdf(self, x_widget, y_widget):
        if self.zoom <= 0:
            return x_widget, y_widget
        return x_widget / self.zoom, y_widget / self.zoom

    # --- Drag selection -----------------------------------------------

    def _on_drag_begin(self, gesture, sx, sy, page_idx):
        x, y = self._to_pdf(sx, sy)
        self._drag_state[page_idx] = {
            "start_x": x, "start_y": y, "cur_x": x, "cur_y": y,
            "start_widget": (sx, sy),
        }

    def _on_drag_update(self, gesture, dx, dy, page_idx):
        ds = self._drag_state.get(page_idx)
        if not ds:
            return
        sx, sy = ds["start_widget"]
        cx, cy = self._to_pdf(sx + dx, sy + dy)
        ds["cur_x"], ds["cur_y"] = cx, cy
        self.page_widgets[page_idx].queue_draw()

    def _on_drag_end(self, gesture, dx, dy, page_idx):
        ds = self._drag_state.pop(page_idx, None)
        if not ds:
            return
        self.page_widgets[page_idx].queue_draw()
        # Tiny drags = treat as a click; the click controller already
        # handled hit-testing, so just bail.
        if abs(dx) + abs(dy) < 4:
            return

        # Pass the actual drag start and end (in document order). Flowing
        # selection needs direction; rectangular bbox would just grab
        # everything between two corners regardless of column layout.
        sel_text, quads = self._extract_selection(
            page_idx,
            ds["start_x"], ds["start_y"],
            ds["cur_x"], ds["cur_y"])
        if not quads:
            return
        # Anchor the popover at the drag-end position in widget coords.
        sx, sy = ds["start_widget"]
        end_widget = (sx + dx, sy + dy)
        self._show_create_popover(page_idx, end_widget, sel_text, quads)

    def _extract_selection(self, page_idx, sx, sy, ex, ey):
        """Return (text, quads) for a flowing text selection between
        the drag start (sx, sy) and end (ex, ey), in widget-Y-down
        PDF points.

        Delegates to Poppler's own selection engine — the same code
        path Evince / GNOME Papers use — instead of re-deriving
        column flow from `get_text_layout()`. Poppler interprets the
        rectangle as "select from the start point to the stop point
        following reading order", not as a bounding box, so the
        multi-column / left-margin-metadata layouts that defeated
        the old heuristic now work.

        `quads` keeps its existing contract: a list of
        `[x, y_top, w, h]` in PDF points, origin top-left, Y-down —
        exactly what `get_selected_region(scale=1.0, …)` yields, so
        hit-testing (`_on_page_click`), the highlight renderer, and
        the sidecar format are all unchanged."""
        try:
            if not (0 <= page_idx < self.doc.get_n_pages()):
                return "", []
            page = self.doc.get_page(page_idx)
        except Exception:
            page = None
        if page is None:
            return "", []

        # Poppler-glib selection uses a top-left origin, Y down, in
        # PDF points at scale 1.0 — the same convention the drag
        # coords are already in. x1,y1 = drag start; x2,y2 = drag
        # end. Poppler orders them by reading flow internally, so we
        # don't pre-sort the corners.
        rect = Poppler.Rectangle()
        rect.x1, rect.y1 = sx, sy
        rect.x2, rect.y2 = ex, ey

        style = Poppler.SelectionStyle.GLYPH
        try:
            sel_text = page.get_selected_text(style, rect) or ""
        except Exception:
            sel_text = ""
        sel_text = sel_text.strip()

        quads = []
        try:
            region = page.get_selected_region(1.0, style, rect)
        except Exception:
            region = None
        if region is not None:
            for i in range(region.num_rectangles()):
                rr = region.get_rectangle(i)
                if rr.width <= 0 or rr.height <= 0:
                    continue
                quads.append([float(rr.x), float(rr.y),
                              float(rr.width), float(rr.height)])

        if not quads:
            return "", []
        return sel_text, quads

    # --- Click on existing highlight ----------------------------------

    def _on_page_click(self, gesture, n_press, x_widget, y_widget, page_idx):
        if n_press != 1:
            return
        x, y = self._to_pdf(x_widget, y_widget)
        # Citation links first — they don't depend on the sidecar and
        # take precedence over highlight hit-testing because publishers
        # place citation rects on tiny [N] glyphs that rarely overlap a
        # highlight anyway.
        if self._handle_citation_click(page_idx, x, y):
            return
        for h in self.highlights:
            if h.get("page") != page_idx:
                continue
            for q in h.get("quads") or []:
                if (q[0] <= x <= q[0] + q[2]
                        and q[1] <= y <= q[1] + q[3]):
                    self._show_edit_popover(h, page_idx,
                                            (x_widget, y_widget))
                    return

    def _citation_at(self, page_idx, x_pdf, y_pdf_down):
        """Return the citation-link entry covering `(x, y)` on this
        page, or None. Link rects from `pdf_links.read_citation_links`
        are in PDF user space (origin bottom-left, y up); incoming
        coords are in the viewer's PDF-points-y-down convention."""
        links = self.citation_links.get(page_idx)
        if not links:
            return None
        _, ph = self.doc.get_page(page_idx).get_size()
        for entry in links:
            rect = entry[0]
            x1, y1_up, x2, y2_up = rect
            x_lo, x_hi = (x1, x2) if x1 <= x2 else (x2, x1)
            y_top_down = ph - max(y1_up, y2_up)
            y_bot_down = ph - min(y1_up, y2_up)
            if (x_lo <= x_pdf <= x_hi
                    and y_top_down <= y_pdf_down <= y_bot_down):
                return entry
        return None

    def _handle_citation_click(self, page_idx, x_pdf, y_pdf_down):
        """Returns True when a citation link was hit and the jump
        was scheduled."""
        entry = self._citation_at(page_idx, x_pdf, y_pdf_down)
        if entry is None:
            return False
        _rect, target_page, target_top, ref_n = entry
        self._jump_to(target_page, target_top)
        # After the jump, surface a popover anchored at the target
        # entry with "Add to library" / "Add + try PDF" actions.
        # Idle-add so layout has scrolled before we anchor — popover
        # positioning uses current widget coordinates.
        if ref_n is not None:
            GLib.idle_add(
                self._show_reference_popover, ref_n, target_page, target_top)
        return True

    def _attach_link_motion_controller(self, da, page_idx):
        """Toggle the pointer cursor over citation links so the user
        knows they're clickable, like Preview's hand cursor. Skip
        pages with no links — no point paying for motion events
        we'd just ignore."""
        if page_idx not in self.citation_links:
            return
        motion = Gtk.EventControllerMotion.new()
        motion.connect(
            "motion",
            lambda c, x, y, _i=page_idx: self._on_link_motion(x, y, _i))
        motion.connect(
            "leave",
            lambda c, _i=page_idx: self._set_link_cursor(_i, False))
        da.add_controller(motion)

    def _on_link_motion(self, x_widget, y_widget, page_idx):
        x, y = self._to_pdf(x_widget, y_widget)
        self._set_link_cursor(
            page_idx, self._citation_at(page_idx, x, y) is not None)

    def _set_link_cursor(self, page_idx, over_link):
        # Only touch the cursor on transitions; motion events fire
        # at pointer-poll rate and most of them stay on the same side
        # of the link boundary.
        if self._cursor_over_link.get(page_idx) == over_link:
            return
        self._cursor_over_link[page_idx] = over_link
        self.page_widgets[page_idx].set_cursor_from_name(
            "pointer" if over_link else None)

    # --- Reference popover (after a citation jump) --------------------

    def _ensure_bibliography_parsed(self):
        """Parse the bibliography on first access and cache the
        result keyed by ref number. Parsing scans the whole PDF, so
        we defer it until the user actually clicks a citation."""
        if self._bibliography_by_n is not None:
            return
        try:
            entries = references_pdf.parse_bibliography(self.pdf_path)
        except Exception:
            entries = []
        self._bibliography_by_n = {e["n"]: e for e in entries}

    def _show_reference_popover(self, ref_n, target_page, target_top):
        """Toolbar-anchored popover for the citation the user just
        jumped to. Shows the parsed entry text immediately, kicks
        off OpenAlex resolution in a background thread, and once
        resolved offers Add-to-library / Add+try-PDF actions.

        target_page / target_top are accepted for signature stability
        (they're how the click handler tells us which entry); the
        popover itself anchors to `self.ref_btn` in the toolbar."""
        del target_page, target_top
        self._ensure_bibliography_parsed()
        entry = self._bibliography_by_n.get(ref_n) or {
            "n": ref_n,
            "text": "(this entry wasn't parsed from the bibliography)",
            "doi": None,
        }

        # Tear down any prior popover so it doesn't linger as an
        # orphan child of `ref_btn`.
        if self._reference_popover is not None:
            try:
                self._reference_popover.popdown()
            except Exception:
                pass
            self._reference_popover = None

        pop = Gtk.Popover()
        pop.set_has_arrow(True)
        pop.set_size_request(460, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)

        # Author-year bibliographies get a "(Sheldrick, 2008)" label
        # — what the user sees in the body text. Numbered keep the
        # "[N]" label.
        if entry.get("surname") and entry.get("year"):
            ref_label = "({}, {}{})".format(
                entry["surname"], entry["year"],
                entry.get("suffix") or "")
        else:
            ref_label = "[{}]".format(ref_n)
        header = Gtk.Label(xalign=0.0)
        header.set_markup(
            "<b>Reference {}</b>".format(
                GLib.markup_escape_text(ref_label)))
        outer.append(header)

        entry_lbl = Gtk.Label(xalign=0.0)
        entry_lbl.set_wrap(True)
        entry_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        entry_lbl.set_max_width_chars(58)
        entry_lbl.set_selectable(True)
        entry_lbl.set_text(entry["text"])
        outer.append(entry_lbl)

        status = Gtk.Label(xalign=0.0)
        status.set_markup(
            "<small><i>Looking up on OpenAlex…</i></small>")
        outer.append(status)

        # Filled in once resolution settles: title/meta + action buttons.
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.append(content)

        pop.set_child(outer)
        self._reference_popover = pop
        # Anchor to the toolbar button. Setting the popover on a
        # MenuButton makes the button toggle it for free, and the
        # button stays visible so the user can re-open the popover
        # after dismissing it.
        self.ref_btn.set_label("Reference {}".format(ref_label))
        self.ref_btn.set_visible(True)
        self.ref_btn.set_popover(pop)
        self.ref_btn.popup()
        # Selectable GtkLabels auto-select-all on focus, so the entry
        # text arrives pre-selected; clear it once after popup so the
        # user starts with no selection but the label stays selectable
        # for on-demand copy-paste.
        GLib.idle_add(lambda: (entry_lbl.select_region(0, 0), False)[1])

        threading.Thread(
            target=self._resolve_reference_and_render,
            args=(entry, pop, status, content),
            daemon=True).start()
        return False  # so this can also be used as a GLib.idle_add target

    def _resolve_reference_and_render(self, entry, popover, status, content):
        resolved = self._resolve_reference_blocking(entry)
        GLib.idle_add(self._render_resolved_reference,
                      popover, status, content, entry, resolved)

    def _resolve_reference_blocking(self, entry):
        """Background-thread resolution. If the parsed entry already
        has a DOI we go straight to OpenAlex by DOI; otherwise we
        ask OpenAlex to resolve the entry by either:

        - author-year search (Acta Cryst, IUCr, older crystallography
          journals): the citation has no title, so we search by
          first-author surname + year + a soft journal-name match.
        - title-based search (numbered/Vancouver-style): the existing
          `metrics.find_doi` path with the heuristic title/author/year
          split of the entry text.

        Returns the normalised metadata dict from
        `metrics.fetch_work_by_doi`, or None if nothing matched."""
        doi = entry.get("doi")
        if not doi and entry.get("surname") and entry.get("year"):
            doi = metrics.find_doi_by_author_year(
                entry["surname"], entry["year"],
                journal=entry.get("journal"))
        if not doi:
            authors_str, year, title, journal = _split_entry_text(
                entry.get("text") or "")
            if title:
                doi = metrics.find_doi(
                    title, year=year,
                    author_names=_author_surnames(authors_str),
                    journal=journal or None)
        if not doi:
            return None
        return metrics.fetch_work_by_doi(doi)

    def _render_resolved_reference(self, popover, status, content,
                                   entry, resolved):
        # Popover may have been replaced or closed in the meantime.
        if popover is not self._reference_popover:
            return False
        if resolved is None:
            status.set_markup(
                "<small><i>Couldn't find this paper on OpenAlex."
                "</i></small>")
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                              spacing=6)
            actions.set_halign(Gtk.Align.END)
            add_btn = Gtk.Button(label="Add as ghost")
            add_btn.set_tooltip_text(
                "Save what we parsed from the PDF as a metadata-only "
                "library entry.")
            add_btn.connect(
                "clicked",
                lambda _b: self._on_add_to_library(popover, entry, None, False))
            actions.append(add_btn)
            content.append(actions)
            return False
        # Check whether the resolved DOI already lives in the library
        # — in which case offering "Add to library" / "Add + try PDF"
        # is misleading. Replace those with a "Show in library" jump
        # button and switch the status line to make it clear.
        existing = None
        resolved_doi = resolved.get("doi")
        if (resolved_doi
                and self.parent_window is not None
                and hasattr(self.parent_window, "find_existing_by_doi")):
            try:
                existing = self.parent_window.find_existing_by_doi(
                    resolved_doi)
            except Exception:
                existing = None

        if existing:
            status.set_markup(
                "<small><span foreground='#2a7a2a'>"
                "✓ Already in your library</span></small>")
        else:
            status.set_markup(
                "<small><span alpha='75%'>Found on OpenAlex:"
                "</span></small>")
        title_lbl = Gtk.Label(xalign=0.0)
        title_lbl.set_wrap(True)
        title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_lbl.set_max_width_chars(58)
        title_lbl.set_markup("<b>{}</b>".format(
            GLib.markup_escape_text(resolved.get("title") or "(untitled)")))
        content.append(title_lbl)
        bits = []
        fa, la = resolved.get("first_author"), resolved.get("last_author")
        if fa and la and fa != la:
            bits.append("{} → {}".format(fa, la))
        elif fa:
            bits.append(fa)
        if resolved.get("year"):
            bits.append(str(resolved["year"]))
        if resolved.get("journal"):
            bits.append(resolved["journal"])
        if resolved.get("citations"):
            bits.append("cited {}×".format(resolved["citations"]))
        if bits:
            meta = Gtk.Label(xalign=0.0)
            meta.set_wrap(True)
            meta.set_max_width_chars(58)
            meta.set_markup(
                "<small><span alpha='75%'>{}</span></small>".format(
                    GLib.markup_escape_text("  ·  ".join(bits))))
            content.append(meta)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_halign(Gtk.Align.END)
        if resolved.get("doi"):
            doi_btn = Gtk.Button(label="Open DOI")
            doi_btn.connect(
                "clicked",
                lambda _b, d=resolved["doi"]:
                    Gio.AppInfo.launch_default_for_uri(
                        "https://doi.org/" + d, None))
            actions.append(doi_btn)
        if existing:
            show_btn = Gtk.Button(label="Show in library")
            show_btn.add_css_class("suggested-action")
            show_btn.connect(
                "clicked",
                lambda _b, p=existing.get("pdf_path"):
                    self._on_show_in_library(popover, p))
            actions.append(show_btn)
        else:
            add_btn = Gtk.Button(label="Add to library")
            add_btn.connect(
                "clicked",
                lambda _b: self._on_add_to_library(
                    popover, entry, resolved, False))
            actions.append(add_btn)
            if resolved.get("is_oa") or resolved.get("oa_url"):
                add_pdf_btn = Gtk.Button(label="Add + try PDF")
                add_pdf_btn.add_css_class("suggested-action")
                add_pdf_btn.set_tooltip_text(
                    "Add to library and try to download the "
                    "open-access PDF via OpenAlex.")
                add_pdf_btn.connect(
                    "clicked",
                    lambda _b: self._on_add_to_library(
                        popover, entry, resolved, True))
                actions.append(add_pdf_btn)
        content.append(actions)
        return False

    def _on_show_in_library(self, popover, pdf_path):
        """"Show in library" button on the citation popover when the
        resolved reference already exists in the library: ask the
        parent BrowserWindow to scroll its cards to the matching
        entry, then dismiss the popover."""
        if (self.parent_window is not None
                and hasattr(self.parent_window, "show_paper_in_library")):
            try:
                self.parent_window.show_paper_in_library(pdf_path)
            except Exception as e:
                print("viewer: show_paper_in_library failed:", e)
        try:
            popover.popdown()
        except Exception:
            pass

    def _on_add_to_library(self, popover, entry, resolved, also_get_pdf):
        if (self.parent_window is None
                or not hasattr(self.parent_window,
                               "add_reference_from_viewer")):
            print("viewer: parent doesn't expose add_reference_from_viewer")
            return
        br = _build_br(entry, resolved)

        def _on_done(success, message, label=None):
            try:
                popover.popdown()
            except Exception:
                pass
            # The browse window's status bar carries the user-visible
            # outcome; this print is just a developer crumb.
            print("viewer: add-reference:", message)
            return False
        self.parent_window.add_reference_from_viewer(
            br, also_get_pdf, _on_done)

    def _jump_to(self, page_idx, top_pdf_up=None):
        """Scroll so that y=`top_pdf_up` (PDF user-space, origin
        bottom-left) on `page_idx` lands at the top of the viewport.
        Falls back to the page's top edge when `top_pdf_up` is None
        or out of range — same effect as `_goto`."""
        if page_idx < 0 or page_idx >= self.n_pages:
            return
        self.current_page = page_idx
        _, ph_pt = self.doc.get_page(page_idx).get_size()
        offset_in_page = 0
        if top_pdf_up is not None and 0 <= top_pdf_up <= ph_pt:
            offset_in_page = (ph_pt - top_pdf_up) * self.zoom
        target_y = self.page_y[page_idx] + offset_in_page

        def _do_scroll():
            adj = self.scrolled.get_vadjustment()
            adj.set_value(target_y)
            return False
        GLib.idle_add(_do_scroll)
        self._update_page_indicator()

    # --- Popovers ------------------------------------------------------

    def _make_popover_at(self, da, widget_xy):
        pop = Gtk.Popover()
        pop.set_parent(da)
        rect = Gdk.Rectangle()
        rect.x = int(widget_xy[0])
        rect.y = int(widget_xy[1])
        rect.width = 1
        rect.height = 1
        pop.set_pointing_to(rect)
        pop.set_has_arrow(True)
        return pop

    def _show_create_popover(self, page_idx, widget_xy, sel_text, quads):
        da = self.page_widgets[page_idx]
        pop = self._make_popover_at(da, widget_xy)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        preview = Gtk.Label(xalign=0.0)
        preview.set_wrap(True)
        preview.set_max_width_chars(60)
        preview.set_markup("<small><i>“{}”</i></small>".format(
            GLib.markup_escape_text(_truncate(sel_text, 220))))
        box.append(preview)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        copy_btn = Gtk.Button(label="Copy")
        copy_btn.set_tooltip_text(
            "Copy the selected text to the clipboard without creating "
            "a highlight.")
        hi_btn = Gtk.Button(label="Highlight")
        hi_btn.add_css_class("suggested-action")
        cm_btn = Gtk.Button(label="Highlight + Comment…")
        cancel_btn = Gtk.Button(label="Cancel")
        btn_row.append(copy_btn)
        btn_row.append(hi_btn)
        btn_row.append(cm_btn)
        btn_row.append(cancel_btn)
        box.append(btn_row)

        pop.set_child(box)

        def _commit(comment_text):
            h = {
                "id": str(uuid.uuid4()),
                "page": page_idx,
                "quads": quads,
                "text": sel_text,
                "color": "yellow",
                "comment": comment_text or "",
                "author": identity.comment_author() if comment_text else "",
                "created": _now_iso(),
                "modified": _now_iso(),
            }
            self.highlights.append(h)
            self._save_highlights()
            da.queue_draw()
            pop.popdown()

        def _copy(_b):
            # Put the extracted selection text on the system
            # clipboard and dismiss — no highlight gets saved.
            try:
                clip = da.get_clipboard()
                clip.set(sel_text or "")
            except Exception as e:
                print("[viewer] clipboard set failed:", e)
            pop.popdown()

        copy_btn.connect("clicked", _copy)
        hi_btn.connect("clicked", lambda _b: _commit(""))
        cm_btn.connect(
            "clicked",
            lambda _b: (pop.popdown(),
                        self._show_comment_editor(page_idx, widget_xy,
                                                  sel_text, quads, None)))
        cancel_btn.connect("clicked", lambda _b: pop.popdown())

        pop.popup()

    def _show_comment_editor(self, page_idx, widget_xy, sel_text, quads,
                             existing):
        """Comment editor popover. `existing` is None for new, else the
        highlight dict to edit in-place."""
        da = self.page_widgets[page_idx]
        pop = self._make_popover_at(da, widget_xy)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        quote = Gtk.Label(xalign=0.0)
        quote.set_wrap(True)
        quote.set_max_width_chars(60)
        quote.set_markup("<small><i>“{}”</i></small>".format(
            GLib.markup_escape_text(_truncate(sel_text, 220))))
        box.append(quote)

        tv = Gtk.TextView()
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_top_margin(4)
        tv.set_left_margin(4)
        tv.set_right_margin(4)
        tv.set_bottom_margin(4)
        if existing:
            tv.get_buffer().set_text(existing.get("comment") or "")
        scr = Gtk.ScrolledWindow()
        scr.set_min_content_width(360)
        scr.set_min_content_height(140)
        scr.set_has_frame(True)
        scr.set_child(tv)
        box.append(scr)

        author_lbl = Gtk.Label(xalign=0.0)
        a = (existing.get("author") if existing else None) or identity.comment_author()
        author_lbl.set_markup(
            "<small>— {}</small>".format(GLib.markup_escape_text(a)))
        box.append(author_lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        cancel_btn = Gtk.Button(label="Cancel")
        btn_row.append(cancel_btn)
        btn_row.append(save_btn)
        box.append(btn_row)

        pop.set_child(box)

        def _save(_b):
            buf = tv.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            if existing is not None:
                existing["comment"] = text
                if text and not existing.get("author"):
                    existing["author"] = identity.comment_author()
                existing["modified"] = _now_iso()
            else:
                self.highlights.append({
                    "id": str(uuid.uuid4()),
                    "page": page_idx,
                    "quads": quads,
                    "text": sel_text,
                    "color": "yellow",
                    "comment": text,
                    "author": identity.comment_author() if text else "",
                    "created": _now_iso(),
                    "modified": _now_iso(),
                })
            self._save_highlights()
            da.queue_draw()
            pop.popdown()

        save_btn.connect("clicked", _save)
        cancel_btn.connect("clicked", lambda _b: pop.popdown())
        pop.popup()
        tv.grab_focus()

    def _show_edit_popover(self, h, page_idx, widget_xy):
        """Open an editor for an existing highlight: shows quoted text,
        comment (editable), author, and a Delete button."""
        da = self.page_widgets[page_idx]
        pop = self._make_popover_at(da, widget_xy)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(8)
        box.set_margin_bottom(8)

        quote = Gtk.Label(xalign=0.0)
        quote.set_wrap(True)
        quote.set_max_width_chars(60)
        quote.set_markup("<small><i>“{}”</i></small>".format(
            GLib.markup_escape_text(_truncate(h.get("text") or "", 220))))
        box.append(quote)

        tv = Gtk.TextView()
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.set_top_margin(4)
        tv.set_left_margin(4)
        tv.set_right_margin(4)
        tv.set_bottom_margin(4)
        tv.get_buffer().set_text(h.get("comment") or "")
        scr = Gtk.ScrolledWindow()
        scr.set_min_content_width(360)
        scr.set_min_content_height(120)
        scr.set_has_frame(True)
        scr.set_child(tv)
        box.append(scr)

        meta = []
        if h.get("author"):
            meta.append("— " + h["author"])
        if h.get("modified"):
            meta.append(h["modified"][:10])
        if meta:
            mlbl = Gtk.Label(xalign=0.0)
            mlbl.set_markup("<small>{}</small>".format(
                GLib.markup_escape_text("  ·  ".join(meta))))
            box.append(mlbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        del_btn = Gtk.Button(label="Delete")
        del_btn.add_css_class("destructive-action")
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        close_btn = Gtk.Button(label="Close")
        btn_row.append(del_btn)
        btn_row.append(close_btn)
        btn_row.append(save_btn)
        box.append(btn_row)

        pop.set_child(box)

        def _save(_b):
            buf = tv.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            h["comment"] = text
            if text and not h.get("author"):
                h["author"] = identity.comment_author()
            h["modified"] = _now_iso()
            self._save_highlights()
            da.queue_draw()
            pop.popdown()

        def _delete(_b):
            try:
                self.highlights.remove(h)
            except ValueError:
                pass
            self._save_highlights()
            da.queue_draw()
            pop.popdown()

        save_btn.connect("clicked", _save)
        del_btn.connect("clicked", _delete)
        close_btn.connect("clicked", lambda _b: pop.popdown())
        pop.popup()

    # --- Errors --------------------------------------------------------

    def _show_error(self, msg):
        lbl = Gtk.Label(label=msg)
        lbl.set_margin_start(20)
        lbl.set_margin_end(20)
        lbl.set_margin_top(20)
        lbl.set_margin_bottom(20)
        self.set_child(lbl)
