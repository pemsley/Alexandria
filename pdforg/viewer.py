"""Built-in PDF viewer using Poppler + GTK4.

v2: continuous scroll, find-in-page, drag-to-select with highlight +
comment annotations. Annotations live in the sidecar JSON (see
sidecar.new_record's `highlights` field); the PDF file itself is
never modified.
"""

import datetime
import os
import uuid

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gtk, Gdk, Gio, GLib, Poppler

from . import sidecar as sidecar_mod, identity


_ZOOM_STEP = 1.25
_ZOOM_MIN = 0.25
_ZOOM_MAX = 8.0
_PAGE_SPACING = 8     # pixels between pages in continuous scroll


_HIGHLIGHT_FILL = (1.0, 0.95, 0.0, 0.35)   # yellow, translucent
_HIGHLIGHT_FILL_HOVER = (1.0, 0.80, 0.0, 0.45)


def _now_iso():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


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

        for w in (first_btn, prev_btn, self.page_entry, self.page_total_lbl,
                  next_btn, last_btn, sep,
                  zoom_out_btn, zoom_reset_btn, zoom_in_btn, zoom_fit_btn,
                  self.zoom_lbl, sep2,
                  self.find_entry, find_prev_btn, find_next_btn,
                  self.find_count_lbl, sep3, self.sidebar_toggle):
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
        """Tiny speech-bubble in the right margin next to the highlight."""
        margin_x = pw + 2     # outside the page, in the spacing
        # Drawn within the page bounds so it stays visible: stick to
        # the right edge.
        x = pw - 14
        y = max(2, quad[1] - 2)
        cr.set_source_rgba(1.0, 0.75, 0.0, 0.95)
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

    def _attach_annotation_controllers(self, da, page_idx):
        """Per-page drag (for selection) and click (for hit-testing existing
        highlights)."""
        drag = Gtk.GestureDrag.new()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin",
                     lambda g, sx, sy, _i=page_idx: self._on_drag_begin(g, sx, sy, _i))
        drag.connect("drag-update",
                     lambda g, dx, dy, _i=page_idx: self._on_drag_update(g, dx, dy, _i))
        drag.connect("drag-end",
                     lambda g, dx, dy, _i=page_idx: self._on_drag_end(g, dx, dy, _i))
        da.add_controller(drag)

        click = Gtk.GestureClick.new()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect(
            "released",
            lambda g, n, x, y, _i=page_idx: self._on_page_click(g, n, x, y, _i))
        da.add_controller(click)

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

    # Same-line tolerance (PDF points) when merging adjacent character
    # rectangles into a single highlight strip.
    _LINE_Y_TOLERANCE = 3.0
    # Minimum x-gap between adjacent characters (in PDF points) for it
    # to count as a column gutter rather than ordinary inter-word space.
    _GUTTER_MIN_PTS = 10.0
    # Line-start x-gap above which we treat two starts as belonging to
    # different columns. Small enough to separate left-margin (x≈35)
    # from right-column body (x≈165), large enough to keep an indented
    # paragraph (x≈185) in the same cluster as the column it belongs to.
    _COLUMN_BREAK_PTS = 30.0

    def _extract_selection(self, page_idx, sx, sy, ex, ey):
        """Return (text, quads) for a flowing text selection between
        the drag start (sx, sy) and end (ex, ey), widget-Y-down PDF
        points.

        Strategy:

        1. `Poppler.Page.get_text_layout()` gives us one rectangle per
           glyph in document order, aligned with `get_text()`'s string.

        2. Detect which *column* the drag is in: collect every char
           whose y falls within the drag's y-range, look at the x
           positions, and find the gutters as the largest gaps. Pick
           the column that contains the drag's x.

        3. Constrain the candidate set to chars in that column, then
           snap drag start / end to the nearest of those chars and
           select the contiguous run between them in document order.

        Steps 2 + 3 together fix the MDPI-style layout where Poppler's
        document order interleaves a left-margin metadata block with
        the right-column body."""
        page = self.doc.get_page(page_idx)
        _, ph = page.get_size()

        try:
            ok, rects = page.get_text_layout()
        except Exception:
            ok, rects = False, []
        if not ok or not rects:
            return "", []
        try:
            full_text = page.get_text() or ""
        except Exception:
            full_text = ""
        if not full_text:
            return "", []

        cap = min(len(rects), len(full_text))
        if cap == 0:
            return "", []

        # ---- Column detection ----------------------------------------
        # Per-line LEFT EDGE positions cluster cleanly into columns
        # (e.g. left-margin lines at x≈35, right-column lines at
        # x≈166). Per-character x positions bridge the gutter visually
        # and don't show a usable gap, so we work at the line level.
        # Bucket each char by its baseline y, take the leftmost x on
        # that line; sort and find the largest gap.
        body_y_lo = ph * 0.10
        body_y_hi = ph * 0.90
        line_min_x = {}        # y_bucket -> min x1 on that line
        for i in range(cap):
            r = rects[i]
            cy = ph - (r.y1 + r.y2) / 2.0
            if not (body_y_lo <= cy <= body_y_hi):
                continue
            y_top = ph - r.y2
            key = round(y_top / 3.0)   # 3-pt buckets glue same line
            x_start = r.x1
            if key not in line_min_x or x_start < line_min_x[key]:
                line_min_x[key] = x_start
        line_starts = sorted(line_min_x.values())

        # Cluster line-starts: same cluster = same column. A "cluster
        # break" is any gap larger than _COLUMN_BREAK_PTS in line-start
        # x. col_lo = cluster's leftmost start (with small tolerance);
        # col_hi = NEXT cluster's leftmost start (so we exclude both
        # halves of an adjacent narrow column / margin block, not just
        # everything left of a midpoint).
        col_lo, col_hi = -1.0e9, 1.0e9
        if len(line_starts) >= 2:
            cluster_mins = [line_starts[0]]
            for x in line_starts[1:]:
                if x - cluster_mins[-1] > self._COLUMN_BREAK_PTS:
                    cluster_mins.append(x)
            if len(cluster_mins) >= 2:
                mid_drag_x = (sx + ex) / 2.0
                for i, cmin in enumerate(cluster_mins):
                    next_cmin = (cluster_mins[i + 1]
                                 if i + 1 < len(cluster_mins)
                                 else 1.0e9)
                    # 50pt slack on the lower side so a drag that
                    # starts just to the left of the column (in the
                    # gutter) still picks the right cluster.
                    if cmin - 50.0 <= mid_drag_x < next_cmin - 5.0:
                        col_lo = cmin - 5.0
                        col_hi = next_cmin - 5.0
                        break

        def in_col(i):
            r = rects[i]
            cx = (r.x1 + r.x2) / 2.0
            return col_lo <= cx <= col_hi

        # ---- Snap drag start / end to nearest in-column chars --------
        start_idx = end_idx = -1
        best_s = best_e = float("inf")
        for i in range(cap):
            if not in_col(i):
                continue
            r = rects[i]
            cx = (r.x1 + r.x2) / 2.0
            cy = ph - (r.y1 + r.y2) / 2.0
            ds = (cx - sx) * (cx - sx) + (cy - sy) * (cy - sy)
            de = (cx - ex) * (cx - ex) + (cy - ey) * (cy - ey)
            if ds < best_s:
                best_s = ds
                start_idx = i
            if de < best_e:
                best_e = de
                end_idx = i
        if start_idx < 0 or end_idx < 0:
            return "", []

        lo = min(start_idx, end_idx)
        hi = max(start_idx, end_idx)

        # ---- Build text + quads from chars[lo..hi] ∩ column ----------
        selected_indices = [i for i in range(lo, hi + 1) if in_col(i)]
        if not selected_indices:
            return "", []

        sel_text = "".join(full_text[i] for i in selected_indices).strip()

        quads = []
        cur = None
        for i in selected_indices:
            r = rects[i]
            w = r.x2 - r.x1
            h = r.y2 - r.y1
            if w <= 0 or h <= 0:
                continue
            x = r.x1
            y_top = ph - r.y2
            if cur is not None and abs(cur[1] - y_top) <= self._LINE_Y_TOLERANCE:
                cur_x_end = cur[0] + cur[2]
                new_x_end = x + w
                cur[0] = min(cur[0], x)
                cur[2] = max(cur_x_end, new_x_end) - cur[0]
                cur[3] = max(cur[3], h)
            else:
                if cur is not None:
                    quads.append(cur)
                cur = [x, y_top, w, h]
        if cur is not None:
            quads.append(cur)

        return sel_text, quads

    # --- Click on existing highlight ----------------------------------

    def _on_page_click(self, gesture, n_press, x_widget, y_widget, page_idx):
        if n_press != 1:
            return
        x, y = self._to_pdf(x_widget, y_widget)
        for h in self.highlights:
            if h.get("page") != page_idx:
                continue
            for q in h.get("quads") or []:
                if (q[0] <= x <= q[0] + q[2]
                        and q[1] <= y <= q[1] + q[3]):
                    self._show_edit_popover(h, page_idx,
                                            (x_widget, y_widget))
                    return

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
        hi_btn = Gtk.Button(label="Highlight")
        hi_btn.add_css_class("suggested-action")
        cm_btn = Gtk.Button(label="Highlight + Comment…")
        cancel_btn = Gtk.Button(label="Cancel")
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
