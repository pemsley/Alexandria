"""Window listing an author's recent works from OpenAlex, with a small
citations-per-year histogram in the header.

Opened from the authors-popover "find more by author" button.
"""

import os
import subprocess
import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Pango

from . import metrics, index


# Hide the histogram entirely if no year reaches this many citations.
HISTOGRAM_MIN_PEAK = 5

HISTOGRAM_WIDTH = 320
HISTOGRAM_HEIGHT = 90


def _open_url(url):
    if not url:
        return
    try:
        subprocess.Popen(["xdg-open", url],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _truncate_authors(names, max_n=4):
    if not names:
        return ""
    if len(names) <= max_n:
        return ", ".join(names)
    return ", ".join(names[:max_n]) + ", et al."


def _draw_histogram(area, cr, width, height, counts_by_year):
    """Bars for cited_by_count per year. counts_by_year is oldest-first."""
    if not counts_by_year:
        return
    peak = max(r["cited_by_count"] for r in counts_by_year) or 1

    style = area.get_style_context()
    fg = style.get_color()  # Gdk.RGBA, theme-aware

    # Layout: leave room at bottom for year labels and a touch on top
    # for the peak label.
    pad_top = 14
    pad_bot = 18
    pad_x = 4
    plot_h = max(1, height - pad_top - pad_bot)
    plot_w = max(1, width - 2 * pad_x)

    n = len(counts_by_year)
    # Bar width with a 2px gap between bars.
    gap = 2
    bw = max(2, (plot_w - (n - 1) * gap) / n)

    # Bars.
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
    for i, r in enumerate(counts_by_year):
        c = r["cited_by_count"]
        if c <= 0:
            continue
        h = plot_h * (c / peak)
        x = pad_x + i * (bw + gap)
        y = pad_top + (plot_h - h)
        cr.rectangle(x, y, bw, h)
        cr.fill()

    # Axis baseline.
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
    cr.set_line_width(1.0)
    cr.move_to(pad_x, pad_top + plot_h + 0.5)
    cr.line_to(pad_x + plot_w, pad_top + plot_h + 0.5)
    cr.stroke()

    # Year labels (first, last, and the peak year).
    cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
    layout = area.create_pango_layout("")
    fd = Pango.FontDescription("Sans 8")
    layout.set_font_description(fd)

    def _draw_text(text, cx, cy, anchor="center"):
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        if anchor == "center":
            cr.move_to(cx - tw / 2, cy)
        elif anchor == "left":
            cr.move_to(cx, cy)
        elif anchor == "right":
            cr.move_to(cx - tw, cy)
        from gi.repository import PangoCairo
        PangoCairo.show_layout(cr, layout)

    first_year = counts_by_year[0]["year"]
    last_year = counts_by_year[-1]["year"]
    peak_idx = max(range(n), key=lambda i: counts_by_year[i]["cited_by_count"])
    peak_year = counts_by_year[peak_idx]["year"]

    y_label_y = pad_top + plot_h + 3
    _draw_text(str(first_year),
               pad_x + bw / 2, y_label_y, "left")
    if last_year != first_year:
        _draw_text(str(last_year),
                   pad_x + plot_w - bw / 2, y_label_y, "right")
    if peak_year not in (first_year, last_year):
        cx = pad_x + peak_idx * (bw + gap) + bw / 2
        _draw_text(str(peak_year), cx, y_label_y, "center")

    # Peak count label, above the tallest bar.
    cx = pad_x + peak_idx * (bw + gap) + bw / 2
    _draw_text(str(peak), cx, 0, "center")


def _existing_dois(conn):
    """Set of normalized DOIs already in our library, lower-cased."""
    out = set()
    try:
        cur = conn.execute(
            "SELECT doi FROM papers WHERE doi IS NOT NULL AND doi<>''")
        for row in cur:
            d = index.normalize_doi(row[0])
            if d:
                out.add(d.lower())
    except Exception:
        pass
    return out


class AuthorWorksWindow(Gtk.Window):
    def __init__(self, parent, conn, authorship):
        super().__init__(transient_for=parent, modal=False)
        self.conn = conn
        self.authorship = authorship or {}
        name = self.authorship.get("name") or "Unknown author"
        self.set_title("Papers by " + name)
        self.set_default_size(720, 720)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)

        # --- Header (name, ORCID, headline numbers) -------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        hleft = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hleft.set_hexpand(True)
        name_lbl = Gtk.Label(xalign=0.0)
        name_lbl.set_markup(
            "<span size='x-large' weight='bold'>{}</span>".format(
                GLib.markup_escape_text(name)))
        name_lbl.set_selectable(True)
        hleft.append(name_lbl)

        sub = []
        if self.authorship.get("orcid"):
            sub.append("ORCID " + self.authorship["orcid"])
        if self.authorship.get("institution"):
            sub.append(self.authorship["institution"])
        if sub:
            sub_lbl = Gtk.Label(xalign=0.0)
            sub_lbl.set_markup(
                "<span size='small' alpha='75%'>{}</span>".format(
                    GLib.markup_escape_text("  ·  ".join(sub))))
            sub_lbl.set_selectable(True)
            hleft.append(sub_lbl)

        self.stats_lbl = Gtk.Label(xalign=0.0)
        self.stats_lbl.set_markup("<span size='small' alpha='65%'>Loading…</span>")
        hleft.append(self.stats_lbl)
        header.append(hleft)

        self.hist_area = Gtk.DrawingArea()
        self.hist_area.set_content_width(HISTOGRAM_WIDTH)
        self.hist_area.set_content_height(HISTOGRAM_HEIGHT)
        self.hist_area.set_visible(False)
        self._hist_data = []
        self.hist_area.set_draw_func(self._on_draw_hist)
        header.append(self.hist_area)

        outer.append(header)

        # Frequent collaborators row (populated async).
        self.coauth_label = Gtk.Label(xalign=0.0)
        self.coauth_label.set_markup(
            "<span size='small' alpha='65%'>Frequent collaborators</span>")
        self.coauth_label.set_visible(False)
        outer.append(self.coauth_label)

        self.coauth_box = Gtk.FlowBox()
        self.coauth_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.coauth_box.set_max_children_per_line(8)
        self.coauth_box.set_row_spacing(4)
        self.coauth_box.set_column_spacing(4)
        self.coauth_box.set_visible(False)
        outer.append(self.coauth_box)

        outer.append(Gtk.Separator())

        # --- Status + results list ------------------------------------
        self.status = Gtk.Label(xalign=0.0)
        self.status.set_markup(
            "<span alpha='75%'>Loading from OpenAlex…</span>")
        outer.append(self.status)

        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                spacing=10)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(self.list_box)
        outer.append(scrolled)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda _b: self.close())
        btn_row.append(close_btn)
        outer.append(btn_row)

        self.set_child(outer)
        self._existing = _existing_dois(self.conn)
        self._spawn_fetch()

    # --- Fetch ---------------------------------------------------------

    def _spawn_fetch(self):
        orcid = self.authorship.get("orcid")
        oa_id = self.authorship.get("openalex_id")
        threading.Thread(
            target=self._do_fetch, args=(orcid, oa_id),
            daemon=True).start()

    def _do_fetch(self, orcid, oa_id):
        profile = metrics.fetch_author_profile(orcid=orcid, openalex_id=oa_id)
        # Use the OpenAlex ID from the profile (if we only had ORCID) so
        # fetch_coauthors can filter the target out reliably.
        target_oa_id = oa_id
        if profile and not target_oa_id:
            # profile dict doesn't carry the ID; re-resolve via authorship.
            pass
        works = metrics.fetch_works_by_author(
            orcid=orcid, openalex_id=oa_id, limit=50)
        coauths = metrics.fetch_coauthors(
            orcid=orcid, openalex_id=oa_id, limit=12)
        GLib.idle_add(self._apply_results, profile, works, coauths)

    def _apply_results(self, profile, works, coauths=None):
        if profile:
            bits = []
            if profile.get("works_count"):
                bits.append("{} works".format(profile["works_count"]))
            if profile.get("cited_by_count"):
                bits.append("{} citations".format(profile["cited_by_count"]))
            if profile.get("h_index") is not None:
                bits.append("h-index {}".format(profile["h_index"]))
            if bits:
                self.stats_lbl.set_markup(
                    "<span size='small' alpha='75%'>{}</span>".format(
                        GLib.markup_escape_text("  ·  ".join(bits))))
            cby = profile.get("counts_by_year") or []
            peak = max((r["cited_by_count"] for r in cby), default=0)
            if peak >= HISTOGRAM_MIN_PEAK:
                self._hist_data = cby
                self.hist_area.set_visible(True)
                self.hist_area.queue_draw()

        if coauths:
            self.coauth_label.set_visible(True)
            self.coauth_box.set_visible(True)
            for c in coauths:
                btn = Gtk.Button()
                btn.set_label("{}  ({})".format(c["name"], c["count"]))
                btn.add_css_class("flat")
                btn.set_tooltip_text(
                    "Open papers by {}".format(c["name"]))
                btn.connect(
                    "clicked",
                    lambda _b, c=c: self._open_coauthor(c))
                self.coauth_box.append(btn)

        if not works:
            self.status.set_markup(
                "<span alpha='75%'>No works found.</span>")
            return
        self.status.set_markup(
            "<span alpha='75%'>{} most recent works</span>".format(len(works)))
        for w in works:
            self.list_box.append(self._make_work_row(w))
        return False

    def _open_coauthor(self, c):
        authorship = {
            "name": c["name"],
            "openalex_id": c["openalex_id"],
            "orcid": None,
            "institution": None,
        }
        open_window(self.get_transient_for() or self, self.conn, authorship)

    # --- Drawing -------------------------------------------------------

    def _on_draw_hist(self, area, cr, width, height):
        _draw_histogram(area, cr, width, height, self._hist_data)

    # --- Per-work row --------------------------------------------------

    def _make_work_row(self, w):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        # Title row (with optional "in library" badge).
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = w.get("title") or "(untitled)"
        title_lbl = Gtk.Label(xalign=0.0)
        title_lbl.set_wrap(True)
        title_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_lbl.set_selectable(True)
        title_lbl.set_hexpand(True)
        title_lbl.set_markup(
            "<b>{}</b>".format(GLib.markup_escape_text(title)))
        title_row.append(title_lbl)

        doi = w.get("doi")
        in_library = bool(doi and doi.lower() in self._existing)
        if in_library:
            badge = Gtk.Label()
            badge.set_markup(
                "<span size='small' foreground='#33aa33'>"
                "<b>✓ in library</b></span>")
            badge.set_valign(Gtk.Align.START)
            title_row.append(badge)
        box.append(title_row)

        # Authors.
        if w.get("authors"):
            auth_lbl = Gtk.Label(xalign=0.0)
            auth_lbl.set_wrap(True)
            auth_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            auth_lbl.set_markup(
                "<span size='small'>{}</span>".format(
                    GLib.markup_escape_text(_truncate_authors(w["authors"]))))
            box.append(auth_lbl)

        # Year · Journal · Type · Citations.
        meta_bits = []
        if w.get("publication_date"):
            meta_bits.append(w["publication_date"])
        elif w.get("year"):
            meta_bits.append(str(w["year"]))
        if w.get("journal"):
            meta_bits.append(w["journal"])
        if w.get("type") and w["type"] != "article":
            meta_bits.append(w["type"])
        if w.get("citations"):
            meta_bits.append("cited {}×".format(w["citations"]))
        if meta_bits:
            meta_lbl = Gtk.Label(xalign=0.0)
            meta_lbl.set_markup(
                "<span size='small' alpha='75%'>{}</span>".format(
                    GLib.markup_escape_text("  ·  ".join(meta_bits))))
            box.append(meta_lbl)

        # Action buttons.
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_top(2)
        if doi:
            b = Gtk.Button(label="DOI")
            b.set_tooltip_text("https://doi.org/" + doi)
            b.connect(
                "clicked",
                lambda _b, d=doi: _open_url("https://doi.org/" + d))
            btn_row.append(b)
        oa_id = w.get("openalex_id")
        if oa_id:
            b = Gtk.Button(label="OpenAlex")
            b.connect(
                "clicked",
                lambda _b, i=oa_id: _open_url("https://openalex.org/" + i))
            btn_row.append(b)
        if w.get("is_oa") and w.get("oa_url"):
            b = Gtk.Button(label="Open-access PDF")
            b.add_css_class("suggested-action")
            b.connect(
                "clicked",
                lambda _b, u=w["oa_url"]: _open_url(u))
            btn_row.append(b)
        if btn_row.get_first_child() is not None:
            box.append(btn_row)

        frame.set_child(box)
        return frame


def open_window(parent, conn, authorship):
    """Create and present an AuthorWorksWindow for the given authorship dict."""
    if not (authorship.get("orcid") or authorship.get("openalex_id")):
        # No usable identifier — caller should have checked, but be safe.
        dlg = Gtk.AlertDialog()
        dlg.set_message(
            "No ORCID or OpenAlex ID available for {}".format(
                authorship.get("name") or "this author"))
        dlg.show(parent)
        return None
    win = AuthorWorksWindow(parent, conn, authorship)
    win.present()
    return win
