"""Window listing an author's recent works from OpenAlex, with a small
citations-per-year histogram in the header.

Opened from the authors-popover "find more by author" button.
"""

import os
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Gio, Pango

from . import metrics, index, importer


def _attach_copy_link_menu(button, url):
    """Attach a right-click context menu with a 'Copy link' entry.
    The button keeps its primary (left-click) action; right-click
    pops up a small menu over the button."""
    if not url:
        return
    action = Gio.SimpleAction.new("copy_url", None)

    def do_copy(_a, _p):
        clipboard = button.get_clipboard()
        try:
            clipboard.set(url)
        except Exception:
            pass

    action.connect("activate", do_copy)
    group = Gio.SimpleActionGroup()
    group.add_action(action)
    button.insert_action_group("ctx", group)

    menu = Gio.Menu()
    menu.append("Copy link", "ctx.copy_url")
    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.set_parent(button)

    gesture = Gtk.GestureClick.new()
    gesture.set_button(Gdk.BUTTON_SECONDARY)

    def on_press(_g, _n, x, y):
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        popover.popup()

    gesture.connect("pressed", on_press)
    button.add_controller(gesture)

LIBRARY_ROOT = os.environ.get(
    "PDFORG_LIBRARY", os.path.expanduser("~/pdfs"))


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


def _filename_for(doi, oa_url):
    """Pick a sensible filename for a downloaded OA PDF. Prefer the URL's
    last path segment when it looks PDF-like; otherwise derive from DOI."""
    if oa_url:
        from urllib.parse import urlparse
        leaf = os.path.basename(urlparse(oa_url).path or "")
        if leaf.lower().endswith(".pdf") and len(leaf) > 4:
            return leaf
    if doi:
        return doi.replace("/", "_") + ".pdf"
    return None


def _download_pdf(url, target_path, timeout=60):
    """Download `url` to `target_path` atomically (.tmp + rename).
    Returns (ok, msg). The returned msg is empty on success and
    a short user-friendly explanation otherwise."""
    tmp = target_path + ".tmp"
    # Pretend to be a normal browser: many publishers (and Cloudflare-
    # protected sites in particular) will 403 anything that doesn't
    # look like one. This still won't get past a JS challenge — see
    # the 403 handler below.
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f, 1 << 16)
    except urllib.error.HTTPError as e:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if e.code == 403 and "cloudflare" in (
                (e.headers.get("server") or "").lower()
                if e.headers else ""):
            return False, ("blocked by Cloudflare bot challenge — "
                           "click View PDF to download in your browser")
        if e.code == 403:
            return False, ("HTTP 403 Forbidden — the publisher refused "
                           "the download. Use View PDF in your browser.")
        if e.code == 404:
            return False, "HTTP 404 — the PDF URL is no longer valid"
        return False, "HTTP {} {}".format(e.code, e.reason or "")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, str(e)
    # Sanity check: real PDFs start with %PDF and are at least a few KB.
    try:
        size = os.path.getsize(tmp)
        with open(tmp, "rb") as f:
            head = f.read(5)
    except OSError as e:
        return False, str(e)
    if size < 1024 or head != b"%PDF-":
        try:
            os.remove(tmp)
        except OSError:
            pass
        # Most likely we got an HTML challenge / login page.
        if head[:5].lower().startswith(b"<htm") or head[:5] == b"<!DOC"[:5]:
            return False, ("server returned HTML, not a PDF — likely "
                           "an anti-bot challenge or login wall")
        return False, "downloaded data isn't a PDF (size={}, head={!r})".format(
            size, head)
    os.rename(tmp, target_path)
    return True, ""


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
            # GtkLabel selects all its text on focus by default. The
            # window opens with this label often the first focusable,
            # so it arrives pre-selected — visually noisy. Clear the
            # selection once after the window is shown; the label
            # stays selectable for on-demand copy-paste.
            GLib.idle_add(
                lambda: (sub_lbl.select_region(0, 0), False)[1])
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

        # --- Sort toggle: most-recent vs most-cited -------------------
        self._works_sort = "recent"
        sort_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        sort_row.set_halign(Gtk.Align.START)
        self._sort_recent_btn = Gtk.ToggleButton(label="Most recent")
        self._sort_cited_btn = Gtk.ToggleButton(label="Most cited")
        # Linked group so they look like a segmented control.
        sort_row.add_css_class("linked")
        self._sort_recent_btn.set_active(True)
        # Group the toggles so exactly one is active at a time.
        self._sort_cited_btn.set_group(self._sort_recent_btn)
        self._sort_recent_btn.connect("toggled", self._on_sort_toggled, "recent")
        self._sort_cited_btn.connect("toggled", self._on_sort_toggled, "cited")
        sort_row.append(self._sort_recent_btn)
        sort_row.append(self._sort_cited_btn)
        outer.append(sort_row)

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
        works = metrics.fetch_works_by_author(
            orcid=orcid, openalex_id=oa_id, limit=50,
            sort=self._works_sort)
        coauths = metrics.fetch_coauthors(
            orcid=orcid, openalex_id=oa_id, limit=12)
        GLib.idle_add(self._apply_results, profile, works, coauths)

    # --- Sort toggle ---------------------------------------------------

    def _on_sort_toggled(self, btn, sort_key):
        # Only react to the *activation* event — the deactivated peer
        # also fires "toggled".
        if not btn.get_active():
            return
        if sort_key == self._works_sort:
            return
        self._works_sort = sort_key
        # Re-fetch with the new sort. Show a placeholder while we wait.
        self.list_box_clear()
        self.status.set_markup(
            "<span alpha='75%'>Re-sorting from OpenAlex…</span>")
        self._spawn_works_only_fetch()

    def list_box_clear(self):
        child = self.list_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list_box.remove(child)
            child = nxt

    def _spawn_works_only_fetch(self):
        """Background refresh of just the works list (skip the
        author-profile and coauthors calls — those don't change with
        the sort choice)."""
        orcid = self.authorship.get("orcid")
        oa_id = self.authorship.get("openalex_id")
        threading.Thread(
            target=self._do_works_only_fetch, args=(orcid, oa_id),
            daemon=True).start()

    def _do_works_only_fetch(self, orcid, oa_id):
        works = metrics.fetch_works_by_author(
            orcid=orcid, openalex_id=oa_id, limit=50,
            sort=self._works_sort)
        GLib.idle_add(self._apply_works_only, works)

    def _apply_works_only(self, works):
        if not works:
            self.status.set_markup(
                "<span alpha='75%'>No works found.</span>")
            return False
        self.status.set_markup(self._works_status_markup(len(works)))
        for w in works:
            self.list_box.append(self._make_work_row(w))
        return False

    def _works_status_markup(self, n):
        sort_label = ("most recent" if self._works_sort == "recent"
                      else "most cited")
        return "<span alpha='75%'>{} {} works</span>".format(n, sort_label)

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
        self.status.set_markup(self._works_status_markup(len(works)))
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
        # OpenAlex disambiguates: pdf_url is the primary direct PDF
        # (best OA location), pdf_urls is *all* known OA mirrors
        # (PMC / repositories / preprint servers), landing_url is the
        # publisher's HTML article page.
        pdf_url = w.get("pdf_url")
        pdf_urls = list(w.get("pdf_urls") or ([pdf_url] if pdf_url else []))
        landing_url = w.get("landing_url") or w.get("oa_url")
        view_target = pdf_url or landing_url
        if w.get("is_oa") and view_target:
            b = Gtk.Button(label="View PDF")
            b.set_tooltip_text(view_target)
            b.connect(
                "clicked",
                lambda _b, u=view_target: _open_url(u))
            _attach_copy_link_menu(b, view_target)
            btn_row.append(b)

            already_in_lib = (doi or "").lower() in self._existing
            add_btn = Gtk.Button(label="Add to Archive")
            add_btn.add_css_class("suggested-action")
            if already_in_lib:
                add_btn.set_label("In library")
                add_btn.set_sensitive(False)
                add_btn.remove_css_class("suggested-action")
            elif not pdf_urls:
                add_btn.set_sensitive(False)
                add_btn.remove_css_class("suggested-action")
                add_btn.set_tooltip_text(
                    "No direct PDF link from OpenAlex (only a publisher "
                    "landing page is available). Use 'View PDF' to open "
                    "it in a browser, then drag the PDF into Alexandria.")
            else:
                tip_lines = ["Download the open-access PDF into your "
                             "library and extract metadata"]
                if len(pdf_urls) > 1:
                    tip_lines.append(
                        "({} mirror{} available)".format(
                            len(pdf_urls),
                            "" if len(pdf_urls) == 1 else "s"))
                add_btn.set_tooltip_text("\n".join(tip_lines))
                add_btn.connect(
                    "clicked",
                    lambda _b, urls=pdf_urls, d=doi, btn=add_btn:
                        self._on_add_to_archive(urls, d, btn))
            btn_row.append(add_btn)
        if btn_row.get_first_child() is not None:
            box.append(btn_row)

        frame.set_child(box)
        return frame

    # ------------------------------------------------------------------
    # Add to Archive: download an OA PDF and import it into the library.
    # ------------------------------------------------------------------

    def _on_add_to_archive(self, urls, doi, btn):
        if not urls:
            return
        # Pick the filename from the first URL (or the DOI fallback).
        fname = _filename_for(doi, urls[0])
        if not fname:
            btn.set_label("No filename")
            btn.set_sensitive(False)
            return

        os.makedirs(LIBRARY_ROOT, exist_ok=True)
        target = os.path.join(LIBRARY_ROOT, fname)

        # Already present? Don't re-download.
        if os.path.exists(target):
            btn.set_label("Already present")
            btn.set_sensitive(False)
            btn.remove_css_class("suggested-action")
            return

        btn.set_sensitive(False)
        btn.set_label("Downloading…")
        threading.Thread(
            target=self._do_add_to_archive,
            args=(urls, target, doi, btn),
            daemon=True,
        ).start()

    def _do_add_to_archive(self, urls, target, doi, btn):
        # Try each candidate in order, falling back to the next on
        # failure. Most papers succeed on the first; CF-protected
        # bioRxiv etc. often have a PMC mirror that works.
        last_msg = ""
        last_url = ""
        for i, url in enumerate(urls):
            if i > 0:
                GLib.idle_add(
                    self._set_add_btn_label, btn,
                    "Trying mirror {} / {}…".format(i + 1, len(urls)))
            ok, msg = _download_pdf(url, target)
            last_msg = msg
            last_url = url
            if ok:
                break
            print("Add to archive: download failed for {}: {}".format(
                url, msg))
        else:
            n = len(urls)
            tail = (" (tried {} mirrors)".format(n) if n > 1 else "")
            GLib.idle_add(
                self._add_to_archive_done, btn, False,
                last_msg + tail, None)
            return

        try:
            rec, status = importer.import_pdf(self.conn, target)
        except Exception as e:
            print("Add to archive: import failed for {}: {}".format(target, e))
            GLib.idle_add(self._add_to_archive_done, btn, False, str(e), None)
            return
        if doi:
            self._existing.add(doi.lower())
        GLib.idle_add(self._add_to_archive_done, btn, True, status, rec)

    def _set_add_btn_label(self, btn, text):
        btn.set_label(text)
        return False

    def _add_to_archive_done(self, btn, ok, status_or_msg, _rec):
        if ok:
            btn.set_label("Added")
            btn.remove_css_class("suggested-action")
            # Already inactive; leave it that way as a record.
        else:
            btn.set_label("Failed — retry?")
            btn.set_tooltip_text("Last error: " + str(status_or_msg))
            btn.set_sensitive(True)
        return False


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
