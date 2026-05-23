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

import datetime

from . import metrics, index, importer, opener
from .identity import maintainer_email
from .markup import safe_pango_markup


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

from . import prefs as _prefs

# Read on import — same shape as `browse.LIBRARY_ROOT`. Previously
# this module read the env var directly with a `~/pdfs` default, which
# ignored the Preferences-set root and dropped "Add to Archive"
# downloads into `~/pdfs` instead of the real library directory.
LIBRARY_ROOT = _prefs.get_library_root()


def _fmt_compact(n):
    """Render a count as `2.1M`, `337k`, `42` etc. Used in the
    citing-impact chip so three numbers fit on one line."""
    if n is None:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000)
    if n >= 10_000:
        return "{:.0f}k".format(n / 1_000)
    if n >= 1_000:
        return "{:.1f}k".format(n / 1_000)
    return str(n)


def _author_score_is_fresh(cached):
    """True when a cached `author_scores` row is younger than the
    TTL. Anything older we'll show briefly then refresh."""
    when = (cached or {}).get("computed_at")
    if not when:
        return False
    try:
        d = datetime.date.fromisoformat(when[:10])
    except ValueError:
        return False
    age_days = (datetime.date.today() - d).days
    return age_days < index.AUTHOR_SCORE_TTL_DAYS


def _author_works_cache_is_fresh(cached):
    """True when a cached `author_works_cache` row is younger than
    its TTL. Stale rows are dropped on read so the next switch
    fetches anew."""
    when = (cached or {}).get("computed_at")
    if not when:
        return False
    try:
        d = datetime.date.fromisoformat(when[:10])
    except ValueError:
        return False
    age_days = (datetime.date.today() - d).days
    return age_days < index.AUTHOR_WORKS_TTL_DAYS


# Venue chips. Some OpenAlex `works` results are legitimately
# different in nature from a journal paper (Zenodo uploads can be
# datasets, slides, code archives, or grey-literature drafts; JoVE
# is a video journal; bioRxiv / arXiv are preprints). A small chip
# in the work row lets the user spot these at a glance instead of
# squinting at the journal name. DOI-prefix match is the primary
# signal; journal-name fallback catches the long tail.
#
# `(label, foreground)` tuples — no background fills, no emojis
# (color emojis crash the user's Cairo/CoreText pipeline on macOS).
_VENUE_CHIPS_BY_DOI_PREFIX = (
    ("10.5281/",   ("Zenodo",  "#b87000")),   # muted orange
    ("10.3791/",   ("JoVE",    "#3366aa")),   # muted blue
    ("10.1101/",   ("bioRxiv", "#777777")),
    ("10.48550/",  ("arXiv",   "#777777")),
    ("10.26434/",  ("ChemRxiv", "#777777")),
    ("10.21203/rs", ("Research Square", "#777777")),
    ("10.22541/au", ("Authorea", "#777777")),
    ("10.2139/ssrn", ("SSRN",   "#777777")),
    ("10.31234/",  ("PsyArXiv", "#777777")),
    ("10.31219/",  ("OSF",     "#777777")),
    ("10.20944/",  ("Preprints.org", "#777777")),
    ("10.36227/",  ("TechRxiv", "#777777")),
)
# bioRxiv and medRxiv share the 10.1101/ prefix; the journal name
# is how OpenAlex disambiguates. Apply this *after* the prefix
# match decided "bioRxiv".
_VENUE_NAME_OVERRIDES = {
    "medrxiv": ("medRxiv", "#777777"),
}


def _venue_chip(work):
    """Decide on at most one venue chip for `work`. Returns
    `(label, foreground)` or None.

    Match order: DOI prefix first (cheap, unambiguous), then a
    journal-name override for the bioRxiv/medRxiv split, then a
    journal-name match for entries OpenAlex stored without a DOI
    in the expected prefix."""
    doi = (work.get("doi") or "").lower()
    journal = (work.get("journal") or "")
    journal_low = journal.lower()
    for prefix, chip in _VENUE_CHIPS_BY_DOI_PREFIX:
        if doi.startswith(prefix):
            # 10.1101/ → bioRxiv default, override to medRxiv when
            # OpenAlex labelled it as such.
            for key, alt in _VENUE_NAME_OVERRIDES.items():
                if key in journal_low:
                    return alt
            return chip
    # No DOI prefix match (or no DOI at all) — fall back to
    # journal-name match. Catches deposits that bypass the usual
    # DOI registrars.
    if journal_low.startswith("zenodo"):
        return ("Zenodo", "#b87000")
    if (journal_low.startswith("journal of visualized experiments")
            or journal_low == "jove"):
        return ("JoVE", "#3366aa")
    return None


# Hide the histogram entirely if no year reaches this many citations.
HISTOGRAM_MIN_PEAK = 5

HISTOGRAM_WIDTH = 320
HISTOGRAM_HEIGHT = 90


def _open_url(url):
    opener.open_external(url)


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


def _curl_download(url, tmp_path, timeout):
    """Subprocess curl fallback. Used when urllib hits a Cloudflare
    403 — Cloudflare TLS-fingerprints Python's `ssl` module and
    rejects it before the request body is even read. System curl
    presents a different TLS ClientHello that Cloudflare accepts.
    Returns (ok, msg). Curl ships in the GNOME Flatpak runtime, so
    this works inside the sandbox too."""
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-L",
             "--max-time", str(int(timeout)),
             "-A", "alexandria/0.1 (mailto:{})".format(maintainer_email()),
             "-o", tmp_path,
             url],
            capture_output=True, timeout=timeout + 5)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, "curl fallback failed: {}".format(e)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return False, "curl: {}".format(err or "exit {}".format(proc.returncode))
    return True, ""


def _download_pdf(url, target_path, timeout=60):
    """Download `url` to `target_path` atomically (.tmp + rename).
    Returns (ok, msg). The returned msg is empty on success and
    a short user-friendly explanation otherwise.

    Tries urllib first; on a Cloudflare 403 (the IUCr / Wiley etc.
    case — TLS-fingerprint rejection rather than a JS challenge),
    retries via subprocess curl which uses a different TLS stack."""
    tmp = target_path + ".tmp"
    # Pretend to be a normal browser: many publishers (and Cloudflare-
    # protected sites in particular) will 403 anything that doesn't
    # look like one.
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
            # Cloudflare rejected the urllib request on TLS
            # fingerprint. Retry via curl, which presents a
            # different ClientHello and is usually accepted.
            ok, curl_msg = _curl_download(url, tmp, timeout)
            if not ok:
                try:
                    if os.path.isfile(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return False, ("blocked by Cloudflare; curl fallback "
                               "also failed ({})".format(curl_msg))
            # Fall through to the PDF sanity-check below.
        else:
            if e.code == 403:
                return False, ("HTTP 403 Forbidden — the publisher refused "
                               "the download. Use View to see the paper in "
                               "your browser.")
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
        # urllib succeeded (HTTP 200) but the body is HTML — a
        # Cloudflare interstitial wrapped in a successful response,
        # not a 403. The IUCr "Radiation damage" case fits this
        # exactly. Retry via curl, which sees the real PDF.
        looks_like_html = (head[:5].lower().startswith(b"<htm")
                           or head[:5] == b"<!DOC"[:5])
        if looks_like_html:
            try:
                os.remove(tmp)
            except OSError:
                pass
            ok, curl_msg = _curl_download(url, tmp, timeout)
            if ok:
                # Re-run the sanity check on whatever curl fetched.
                try:
                    size = os.path.getsize(tmp)
                    with open(tmp, "rb") as f:
                        head = f.read(5)
                except OSError as e:
                    return False, str(e)
                if size >= 1024 and head == b"%PDF-":
                    os.rename(tmp, target_path)
                    return True, ""
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False, ("server returned HTML, not a PDF — likely "
                           "an anti-bot challenge or login wall; curl "
                           "fallback also did not yield a PDF")
        try:
            os.remove(tmp)
        except OSError:
            pass
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

        # Sub line: ORCID · current institution + small history button
        # that opens a popover with the full prior-institution list.
        # The label gets filled in once the profile arrives — for
        # callers that don't pass `institution` in the authorship
        # (e.g. Discover) the line stays sparse until then.
        sub_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._sub_orcid_lbl = Gtk.Label(xalign=0.0)
        self._sub_orcid_lbl.set_selectable(True)
        self._sub_orcid_lbl.set_visible(False)
        if self.authorship.get("orcid"):
            self._sub_orcid_lbl.set_markup(
                "<span size='small' alpha='75%'>ORCID {}</span>".format(
                    GLib.markup_escape_text(self.authorship["orcid"])))
            self._sub_orcid_lbl.set_visible(True)
        sub_row.append(self._sub_orcid_lbl)

        self._sub_inst_lbl = Gtk.Label(xalign=0.0)
        self._sub_inst_lbl.set_visible(False)
        # Long affiliations (especially CrossRef's full department +
        # school + university + city strings) would otherwise force the
        # whole window wide. Wrap on word/comma boundaries and cap the
        # requested width so the line flows to multiple rows instead.
        self._sub_inst_lbl.set_wrap(True)
        self._sub_inst_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._sub_inst_lbl.set_max_width_chars(60)
        if self.authorship.get("institution"):
            self._sub_inst_lbl.set_markup(
                "<span size='small' alpha='75%'>·  {}</span>".format(
                    GLib.markup_escape_text(self.authorship["institution"])))
            self._sub_inst_lbl.set_visible(True)
        sub_row.append(self._sub_inst_lbl)

        self._aff_history_btn = Gtk.MenuButton()
        self._aff_history_btn.set_icon_name("document-open-recent-symbolic")
        self._aff_history_btn.add_css_class("flat")
        self._aff_history_btn.set_tooltip_text(
            "Show all prior institutions")
        self._aff_history_btn.set_visible(False)
        sub_row.append(self._aff_history_btn)

        # Clear any auto-selection once after present (selectable
        # labels grab focus and select-all by default).
        GLib.idle_add(
            lambda: (self._sub_orcid_lbl.select_region(0, 0), False)[1])
        hleft.append(sub_row)

        self.stats_lbl = Gtk.Label(xalign=0.0)
        self.stats_lbl.set_markup("<span size='small' alpha='65%'>Loading…</span>")
        hleft.append(self.stats_lbl)

        # Citing-impact chip: three-bucket (software/method/idea)
        # rollup of citations to this author's works, computed
        # lazily and cached for 30 days in `author_scores`. Hidden
        # until we have an OpenAlex ID and a result to show.
        self.citing_impact_lbl = Gtk.Label(xalign=0.0)
        self.citing_impact_lbl.set_use_markup(True)
        self.citing_impact_lbl.set_visible(False)
        hleft.append(self.citing_impact_lbl)

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

        # Refresh button — drops both sort caches for this author
        # and re-fetches. Sits to the right of the segmented sort
        # control with a small gap; icon-only so it doesn't compete
        # for attention with the sort labels.
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text(
            "Re-fetch works from OpenAlex (clears the 7-day cache "
            "for this author).")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_margin_start(8)
        refresh_btn.connect("clicked", self._on_refresh_works)
        sort_row.append(refresh_btn)

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
        # Citing-impact lives on its own thread because the compute
        # is slow (~3 min for prolific authors) and we don't want
        # to block the works list on it. Cache-hits return in
        # microseconds; cache-misses do the slow OpenAlex walk.
        if oa_id:
            threading.Thread(
                target=self._do_citing_impact, args=(oa_id,),
                daemon=True).start()

    def _do_fetch(self, orcid, oa_id):
        profile = metrics.fetch_author_profile(orcid=orcid, openalex_id=oa_id)
        works = self._cached_or_fetch_works(orcid, oa_id, self._works_sort)
        coauths = metrics.fetch_coauthors(
            orcid=orcid, openalex_id=oa_id, limit=12)
        GLib.idle_add(self._apply_results, profile, works, coauths)

    def _cached_or_fetch_works(self, orcid, oa_id, sort_key):
        """Try the works-list cache first; on hit and fresh, return
        it. Otherwise hit OpenAlex and persist. Cache is keyed off
        the OpenAlex author ID — ORCID-only authors miss the cache,
        but `fetch_works_by_author` then yields the OpenAlex ID via
        its results and we can't backfill cleanly here. Acceptable
        v1: ORCID-only callers always go to the API."""
        if oa_id:
            try:
                cached = index.get_author_works_cache(
                    self.conn, oa_id, sort_key)
            except Exception:
                cached = None
            if cached and _author_works_cache_is_fresh(cached):
                return cached["works"]
        works = metrics.fetch_works_by_author(
            orcid=orcid, openalex_id=oa_id, limit=50, sort=sort_key)
        if oa_id and works:
            try:
                index.set_author_works_cache(
                    self.conn, oa_id, sort_key, works)
            except Exception as e:
                print("[author_works] cache write failed:", e)
        return works

    # --- Citing-impact (cached) ----------------------------------------

    def _do_citing_impact(self, openalex_id):
        cached = index.get_author_score(self.conn, openalex_id)
        if cached and _author_score_is_fresh(cached):
            GLib.idle_add(self._apply_citing_impact, cached, False)
            return
        # Stale or absent — show a "computing" placeholder, then
        # the real numbers. Stale rows are still shown briefly so
        # the user has something to look at while the refresh runs.
        if cached:
            GLib.idle_add(self._apply_citing_impact, cached, True)
        else:
            GLib.idle_add(self._show_citing_impact_pending)
        result = metrics.compute_citing_impact(
            openalex_id, exclude_self_cites=True, polite_delay=0.0)
        if not result:
            GLib.idle_add(self._hide_citing_impact_pending)
            return
        try:
            index.set_author_score(self.conn, openalex_id, result,
                                   self_excluded=True)
        except Exception:
            pass
        GLib.idle_add(self._apply_citing_impact, result, False)

    def _show_citing_impact_pending(self):
        self.citing_impact_lbl.set_markup(
            "<span size='small' alpha='55%'>"
            "Citing-impact: computing…"
            "</span>")
        self.citing_impact_lbl.set_visible(True)
        return False

    def _hide_citing_impact_pending(self):
        # Only hide if we're still on the pending placeholder.
        # The user might have a stale-but-rendered result behind us.
        if "computing" in (self.citing_impact_lbl.get_text() or ""):
            self.citing_impact_lbl.set_visible(False)
        return False

    def _apply_citing_impact(self, result, is_stale):
        if not result:
            return False
        bits = []
        for kind in ("software", "method", "idea"):
            b = result.get(kind) or {}
            total = b.get("total") or 0
            n_works = b.get("n_works") or 0
            if n_works == 0:
                continue
            bits.append("{} {}".format(kind, _fmt_compact(total)))
        if not bits:
            self.citing_impact_lbl.set_visible(False)
            return False
        stale_marker = " (stale)" if is_stale else ""
        self.citing_impact_lbl.set_markup(
            "<span size='small' alpha='65%'>"
            "Citing-impact: {}{}</span>".format(
                GLib.markup_escape_text("  ·  ".join(bits)),
                GLib.markup_escape_text(stale_marker)))
        # Build a tooltip with the per-bucket breakdown so the
        # compact chip stays compact but the detail is one hover
        # away.
        tip_lines = ["Citations of papers that cite this author's "
                     "works (self-cites excluded), bucketed by the "
                     "kind of work being cited."]
        for kind in ("software", "method", "idea"):
            b = result.get(kind) or {}
            n_works = b.get("n_works") or 0
            if n_works == 0:
                continue
            total = b.get("total") or 0
            n_citing = b.get("n_citing") or 0
            mean = (total / n_citing) if n_citing else 0.0
            tip_lines.append(
                "{}: {} works, {} citing papers, "
                "{} total cites of citers, mean {:.1f}".format(
                    kind.capitalize(),
                    n_works,
                    "{:,}".format(n_citing),
                    "{:,}".format(total),
                    mean))
        when = result.get("computed_at")
        if when:
            tip_lines.append("Computed {}.".format(when))
        self.citing_impact_lbl.set_tooltip_text("\n".join(tip_lines))
        self.citing_impact_lbl.set_visible(True)
        return False

    # --- Sort toggle ---------------------------------------------------

    def _on_refresh_works(self, _btn):
        """Drop the cache for this author and re-fetch the current
        sort. The other sort's cache is dropped too, so flipping
        the toggle after a refresh also goes to OpenAlex."""
        oa_id = self.authorship.get("openalex_id")
        if oa_id:
            try:
                index.clear_author_works_cache(self.conn, oa_id)
            except Exception as e:
                print("[author_works] cache clear failed:", e)
        self.list_box_clear()
        self.status.set_markup(
            "<span alpha='75%'>Refreshing from OpenAlex…</span>")
        self._spawn_works_only_fetch()

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
        works = self._cached_or_fetch_works(orcid, oa_id, self._works_sort)
        GLib.idle_add(self._apply_works_only, works)

    def _apply_works_only(self, works):
        if not works:
            self.status.set_markup(self._empty_status_markup())
            return False
        self.status.set_markup(self._works_status_markup(len(works)))
        for w in works:
            self.list_box.append(self._make_work_row(w))
        return False

    def _empty_status_markup(self):
        """Pick the right "nothing to show" message. When the
        OpenAlex session breaker is tripped, the empty result is
        a rate-limit symptom, not "the author really has no
        works" — say so explicitly."""
        if metrics.openalex_paused_until() > 0:
            return ("<span foreground='#cc6633'>Search blocked by "
                    "OpenAlex — daily quota exhausted. Resumes at "
                    "00:00 UTC.</span>")
        return "<span alpha='75%'>No works found.</span>"

    def _works_status_markup(self, n):
        sort_label = ("most recent" if self._works_sort == "recent"
                      else "most cited")
        return "<span alpha='75%'>{} {} works</span>".format(n, sort_label)

    def _apply_results(self, profile, works, coauths=None):
        # OpenAlex circuit breaker tripped → profile is None and
        # works is []. Surface the rate-limit reason rather than
        # leaving the "Loading…" line stuck and saying "No works
        # found" on the empty body.
        if not profile and not works and metrics.openalex_paused_until() > 0:
            blocked = ("<span size='small' foreground='#cc6633'>"
                       "Search blocked by OpenAlex — daily quota "
                       "exhausted. Resumes at 00:00 UTC.</span>")
            self.stats_lbl.set_markup(blocked)
            self.status.set_markup(blocked)
            return
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

            self._populate_affiliations(profile.get("affiliations") or [])

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
            self.status.set_markup(self._empty_status_markup())
            return
        self.status.set_markup(self._works_status_markup(len(works)))
        for w in works:
            self.list_box.append(self._make_work_row(w))
        return False

    # --- Affiliations -------------------------------------------------

    def _populate_affiliations(self, rows):
        """Set the header's "current institution" label to the most
        recent affiliation, and stash the full list behind a popover
        on `self._aff_history_btn`. Hides the button if the list is
        empty or the author has only one institution (no history to
        show)."""
        if not rows:
            return
        # Most-recent first comes from metrics.fetch_author_profile;
        # row[0] is "where they are now" with the usual asterisk.
        current = rows[0]
        if not self._sub_inst_lbl.get_visible():
            self._sub_inst_lbl.set_markup(
                "<span size='small' alpha='75%'>·  {}</span>".format(
                    GLib.markup_escape_text(current["display_name"])))
            self._sub_inst_lbl.set_visible(True)

        if len(rows) <= 1:
            return  # nothing extra to surface
        # Build the popover contents — a vertically scrollable list
        # of "year_range  institution_name" lines.
        pop = Gtk.Popover()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        hdr = Gtk.Label(xalign=0.0)
        hdr.set_markup(
            "<b>Prior institutions</b>  "
            "<span alpha='65%' size='small'>({})</span>".format(len(rows)))
        outer.append(hdr)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(min(360, 26 * len(rows) + 10))
        scrolled.set_min_content_width(420)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        for r in rows:
            body.append(self._make_aff_row(r))
        scrolled.set_child(body)
        outer.append(scrolled)
        pop.set_child(outer)
        self._aff_history_btn.set_popover(pop)
        self._aff_history_btn.set_visible(True)

    def _make_aff_row(self, r):
        ymin, ymax = r["year_min"], r["year_max"]
        years = "{}".format(ymin) if ymin == ymax else "{}–{}".format(ymin, ymax)
        lbl = Gtk.Label(xalign=0.0)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_max_width_chars(80)
        lbl.set_markup(
            "<span size='small'>"
            "<tt>{:>9}</tt>  <span alpha='80%'>{}</span>"
            "</span>".format(
                years, GLib.markup_escape_text(r["display_name"])))
        return lbl

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
        from .browse import _title_color
        title_lbl.set_markup(
            "<span foreground='{}'><b>{}</b></span>".format(
                _title_color(self), safe_pango_markup(title)))
        title_row.append(title_lbl)

        # Retracted chip — highest-priority warning, drawn first
        # so it sits closest to the title. OpenAlex carries the
        # flag directly on the work, so this is free.
        if w.get("is_retracted"):
            rl = Gtk.Label()
            rl.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "<b>⚠ RETRACTED</b></span>")
            rl.set_valign(Gtk.Align.START)
            rl.set_tooltip_text(
                "OpenAlex flags this work as retracted.")
            title_row.append(rl)

        # Paratext chip — editorials, tables of contents, masthead
        # entries. Muted so it doesn't shout for attention; it's
        # mostly a "this isn't a real paper" hint.
        if w.get("is_paratext"):
            pl = Gtk.Label()
            pl.set_markup(
                "<span size='small' foreground='#888888'>"
                "<b>paratext</b></span>")
            pl.set_valign(Gtk.Align.START)
            pl.set_tooltip_text(
                "Editorial / table-of-contents / masthead entry "
                "rather than a research article.")
            title_row.append(pl)

        # Preprint badge — OpenAlex work type, or a DOI on a known
        # preprint server (bioRxiv / arXiv / chemRxiv / …). Same
        # "PRE" styling as the library card's preprint chip.
        if (w.get("type") == "preprint"
                or metrics.is_preprint_doi(w.get("doi"))):
            pre = Gtk.Label()
            pre.set_markup(
                "<span size='small' foreground='#cc6600'>"
                "<b>PRE</b></span>")
            pre.set_valign(Gtk.Align.START)
            pre.set_tooltip_text("Preprint")
            title_row.append(pre)

        # Venue chip (Zenodo / JoVE / bioRxiv / arXiv / ...). One
        # at most. Sits between the title and the in-library
        # badge, right-aligned visually because the title's
        # hexpand pushes it.
        chip = _venue_chip(w)
        if chip:
            label, fg = chip
            chip_lbl = Gtk.Label()
            chip_lbl.set_markup(
                "<span size='small' foreground='{}'>"
                "<b>{}</b></span>".format(
                    fg, GLib.markup_escape_text(label)))
            chip_lbl.set_valign(Gtk.Align.START)
            title_row.append(chip_lbl)

        # License / OA chip. Same chip factories as the main card
        # in browse.py — lazy import to dodge the circular
        # browse↔author_works dependency. Free data (already in the
        # OpenAlex response we just consumed).
        from .browse import make_license_chip, make_oa_chip
        lic_chip = make_license_chip(w.get("license_label"), None)
        if lic_chip is not None:
            lic_chip.set_valign(Gtk.Align.START)
            title_row.append(lic_chip)
        else:
            oa_chip = make_oa_chip(w.get("is_oa"), w.get("oa_status"))
            if oa_chip is not None:
                oa_chip.set_valign(Gtk.Align.START)
                title_row.append(oa_chip)

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

        # Funders. Up to two displayed; rest collapse to "+N more"
        # so the row doesn't grow unbounded for heavily-funded
        # consortium papers. Award IDs go into the tooltip.
        grants = w.get("grants") or []
        if grants:
            visible = grants[:2]
            extra = len(grants) - len(visible)
            funder_bits = [g["funder"] for g in visible]
            text = "Funded by " + ", ".join(funder_bits)
            if extra > 0:
                text += " · +{} more".format(extra)
            tip_parts = []
            for g in grants:
                if g.get("award_id"):
                    tip_parts.append("{} ({})".format(g["funder"],
                                                       g["award_id"]))
                else:
                    tip_parts.append(g["funder"])
            funder_lbl = Gtk.Label(xalign=0.0)
            funder_lbl.set_wrap(True)
            funder_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            funder_lbl.set_markup(
                "<span size='small' alpha='75%'>{}</span>".format(
                    GLib.markup_escape_text(text)))
            funder_lbl.set_tooltip_text("\n".join(tip_parts))
            box.append(funder_lbl)

        # Year · Journal · Type · Citations · FWCI · Topic.
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
        # FWCI — Field-Weighted Citation Impact. >1 means above the
        # field average; we render with 2 dp because the value is
        # usually 0.05–10ish. Skip when missing or zero (zero often
        # means "too new to score" rather than "actually zero").
        fwci = w.get("fwci")
        if isinstance(fwci, (int, float)) and fwci > 0:
            meta_bits.append("FWCI {:.2f}".format(fwci))
        if w.get("top_topic"):
            meta_bits.append(w["top_topic"])
        if meta_bits:
            meta_lbl = Gtk.Label(xalign=0.0)
            meta_lbl.set_markup(
                "<span size='small' alpha='75%'>{}</span>".format(
                    GLib.markup_escape_text("  ·  ".join(meta_bits))))
            meta_lbl.set_wrap(True)
            meta_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
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
            b = Gtk.Button(label="View")
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
                    "landing page is available). Use 'View' to open "
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
