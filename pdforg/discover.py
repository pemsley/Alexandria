"""Discover — search OpenAlex from a blank canvas.

Two modes:

* **By author**: name + optional institution + optional ORCID. Returns
  candidate authors with affiliation and top-topic chips. Click an
  author → opens the existing AuthorWorksWindow (works list with
  per-paper "Add to library" buttons).

* **By topic**: free-text query, optional year-min filter, sort by
  relevance / citations / recency. Returns paper rows; per-row "Add
  to library" + DOI buttons.

This is the v0 entry point for an empty library — the user can find
something to import without first having any papers in the index.
"""

import re
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango

from . import author_works, metrics


def open_window(parent, conn):
    """Show a Discover window. `parent` is the BrowserWindow — we hand
    "Add to library" requests back to `parent.add_reference_from_viewer`,
    so that path stays the single ghost-import entry point."""
    win = DiscoverWindow(parent, conn)
    win.present()
    return win


class DiscoverWindow(Adw.Window):
    def __init__(self, parent_window, conn):
        super().__init__()
        self.set_transient_for(parent_window)
        self.parent_window = parent_window
        self.conn = conn
        self.set_title("Discover")
        self.set_default_size(820, 640)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        outer.append(header)

        # ── Mode tabs ─────────────────────────────────────────────
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)

        # add_titled_with_icon: Adw.ViewSwitcher always renders a
        # per-page icon, so without one each tab shows the broken-image
        # placeholder. Icons are standard Adwaita symbolics.
        self.stack.add_titled_with_icon(
            self._build_author_page(), "author", "By author",
            "avatar-default-symbolic")
        self.stack.add_titled_with_icon(
            self._build_topic_page(), "topic", "By topic",
            "system-search-symbolic")
        self.stack.add_titled_with_icon(
            self._build_title_page(), "title", "By title",
            "text-x-generic-symbolic")
        self.stack.add_titled_with_icon(
            self._build_pdb_page(), "pdb", "By PDB",
            "applications-science-symbolic")

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        outer.append(self.stack)
        self.set_content(outer)

    # =========================================================
    # By author
    # =========================================================

    def _build_author_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        # Search controls.
        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._a_name = Gtk.Entry()
        self._a_inst = Gtk.Entry()
        self._a_orcid = Gtk.Entry()
        controls.append(_form_row("Name",        self._a_name))
        controls.append(_form_row("Institution", self._a_inst))
        controls.append(_form_row("ORCID",       self._a_orcid))
        self._a_name.set_placeholder_text("e.g. William B. Smith")
        self._a_inst.set_placeholder_text(
            "optional — e.g. Stanford  (resolved to top-matching institution)")
        self._a_orcid.set_placeholder_text(
            "optional fast-path — e.g. 0000-0002-1825-0097")

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._a_search_btn = Gtk.Button(label="Search OpenAlex")
        self._a_search_btn.add_css_class("suggested-action")
        self._a_search_btn.connect("clicked", self._on_author_search)
        self._a_show_stubs = Gtk.CheckButton(
            label="Show authors with 0 works")
        self._a_show_stubs.set_active(False)
        self._a_show_stubs.connect(
            "toggled", lambda _b: self._render_authors())
        btn_row.append(self._a_search_btn)
        btn_row.append(self._a_show_stubs)
        controls.append(btn_row)

        for entry in (self._a_name, self._a_inst, self._a_orcid):
            entry.connect("activate", self._on_author_search)

        box.append(controls)

        self._a_status = Gtk.Label(xalign=0.0)
        box.append(self._a_status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self._a_results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scrolled.set_child(self._a_results_box)
        box.append(scrolled)

        # Last raw result list (unfiltered) — re-rendered when the
        # "show stubs" toggle flips so we don't re-hit the network.
        self._a_last_results = []
        self._a_last_query = None
        return box

    def _on_author_search(self, _btn):
        name = (self._a_name.get_text() or "").strip()
        inst = (self._a_inst.get_text() or "").strip() or None
        orcid = (self._a_orcid.get_text() or "").strip() or None
        if not (name or orcid):
            self._a_status.set_text("Enter a name or an ORCID.")
            return
        self._a_status.set_text("Searching OpenAlex…")
        self._a_search_btn.set_sensitive(False)
        self._clear_box(self._a_results_box)
        self._a_last_query = (name, inst, orcid)

        def _do():
            try:
                rows = metrics.search_authors(
                    name=name, institution=inst, orcid=orcid, limit=20)
            except Exception as e:
                rows = []
                err = str(e)
            else:
                err = None
            GLib.idle_add(self._after_author_search, rows, err)

        threading.Thread(target=_do, daemon=True).start()

    def _after_author_search(self, rows, err):
        self._a_search_btn.set_sensitive(True)
        if err:
            self._a_status.set_markup(
                "<span foreground='#cc3333'>Search failed: {}</span>".format(
                    GLib.markup_escape_text(err)))
            return False
        self._a_last_results = rows
        self._render_authors()
        return False

    def _render_authors(self):
        self._clear_box(self._a_results_box)
        rows = self._a_last_results
        show_stubs = self._a_show_stubs.get_active()
        visible = [r for r in rows if show_stubs or (r.get("works_count") or 0) > 0]

        # Status line: matched-institution context + counts.
        bits = []
        if rows and rows[0].get("matched_institution"):
            mi = rows[0]["matched_institution"]
            bits.append("institution resolved to <b>{}</b>".format(
                GLib.markup_escape_text(mi.get("display_name") or "?")))
        if not rows:
            bits.append("no matches")
        else:
            hidden = len(rows) - len(visible)
            bits.append("{} match{}".format(
                len(visible), "" if len(visible) == 1 else "es"))
            if hidden and not show_stubs:
                bits.append(
                    "<span alpha='65%'>({} stub record{} hidden)</span>".format(
                        hidden, "" if hidden == 1 else "s"))
        self._a_status.set_markup("<small>" + " · ".join(bits) + "</small>")

        for r in visible:
            self._a_results_box.append(self._build_author_row(r))

    def _build_author_row(self, r):
        """Build one author result row. The whole row is a button —
        click anywhere on it to open the author-works window. A
        'go-next' chevron on the right makes the affordance obvious;
        the row hover-highlights so it doesn't look static."""
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        outer.set_margin_start(10)
        outer.set_margin_end(10)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)

        # Name (bold) + ORCID chip.
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nm = Gtk.Label(xalign=0.0)
        nm.set_markup("<b>{}</b>".format(
            GLib.markup_escape_text(r.get("display_name") or "(unnamed)")))
        name_row.append(nm)
        if r.get("orcid"):
            orcid_lbl = Gtk.Label()
            orcid_lbl.set_markup(
                "<small><span alpha='65%'>ORCID {}</span></small>".format(
                    GLib.markup_escape_text(r["orcid"])))
            name_row.append(orcid_lbl)
        info.append(name_row)

        # Institution line.
        inst = r.get("last_known_institution") or "(no recent affiliation)"
        meta = Gtk.Label(xalign=0.0)
        meta.set_wrap(True)
        meta.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        meta.set_max_width_chars(70)
        meta.set_markup(
            "<small><span alpha='75%'>{}</span></small>".format(
                GLib.markup_escape_text(inst)))
        info.append(meta)

        # Stats + topic chip.
        stats_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        stats = Gtk.Label(xalign=0.0)
        stats.set_markup(
            "<small>{} works · cited {}x</small>".format(
                r.get("works_count") or 0, r.get("cited_by_count") or 0))
        stats_row.append(stats)
        if r.get("top_topic"):
            chip = Gtk.Label()
            # Mid-purple — the original `#5a2a82` was rich on light
            # backgrounds but unreadable on Adwaita dark. A brighter
            # tone holds against both extremes.
            chip.set_markup(
                "<small><span foreground='#a06acc'>★ {}</span></small>".format(
                    GLib.markup_escape_text(r["top_topic"])))
            stats_row.append(chip)
        info.append(stats_row)

        outer.append(info)

        # Affordance: chevron + label so it's obvious the row is
        # clickable, not just informational.
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        right.set_valign(Gtk.Align.CENTER)
        right.append(Gtk.Label(label="Show works"))
        right.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        outer.append(right)

        # Whole-row button: click anywhere to drill in.
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.set_child(outer)
        btn.set_tooltip_text(
            "Open papers by " + (r.get("display_name") or "this author"))
        btn.connect(
            "clicked", lambda _b, rr=r: self._open_author_works(rr))
        return btn

    def _open_author_works(self, r):
        """Hand off to the existing AuthorWorksWindow. We synthesise the
        authorship dict shape that author_works.open_window expects."""
        authorship = {
            "name": r.get("display_name"),
            "orcid": r.get("orcid"),
            "openalex_id": r.get("openalex_id"),
        }
        author_works.open_window(self.parent_window, self.conn, authorship)

    # =========================================================
    # By topic
    # =========================================================

    def _build_topic_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._t_query = Gtk.Entry()
        controls.append(_form_row("Query", self._t_query))
        self._t_query.set_placeholder_text(
            "e.g. antibiotic resistance, glycogen branching enzyme, …")

        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opts.append(Gtk.Label(label="Year ≥"))
        self._t_year = Gtk.Entry()
        self._t_year.set_max_length(4)
        self._t_year.set_width_chars(5)
        self._t_year.set_placeholder_text("(any)")
        opts.append(self._t_year)
        opts.append(Gtk.Label(label="  Sort"))
        sl = Gtk.StringList()
        for label in ("Relevance", "Most cited", "Most recent"):
            sl.append(label)
        self._t_sort = Gtk.DropDown(model=sl)
        self._t_sort.set_selected(1)   # Most cited — usually most useful default
        opts.append(self._t_sort)
        controls.append(opts)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._t_search_btn = Gtk.Button(label="Search OpenAlex")
        self._t_search_btn.add_css_class("suggested-action")
        self._t_search_btn.connect("clicked", self._on_topic_search)
        btn_row.append(self._t_search_btn)
        controls.append(btn_row)

        self._t_query.connect("activate", self._on_topic_search)

        box.append(controls)

        self._t_status = Gtk.Label(xalign=0.0)
        box.append(self._t_status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self._t_results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scrolled.set_child(self._t_results_box)
        box.append(scrolled)
        return box

    def _on_topic_search(self, _btn):
        query = (self._t_query.get_text() or "").strip()
        if not query:
            self._t_status.set_text("Enter a query.")
            return
        year_text = (self._t_year.get_text() or "").strip()
        year_min = None
        if year_text:
            try:
                year_min = int(year_text)
            except ValueError:
                self._t_status.set_text("Year must be a number.")
                return
        sort_idx = self._t_sort.get_selected()
        sort = ("relevance", "cited", "recent")[max(0, min(sort_idx, 2))]

        self._t_status.set_text("Searching OpenAlex…")
        self._t_search_btn.set_sensitive(False)
        self._clear_box(self._t_results_box)

        def _do():
            try:
                rows = metrics.search_works(
                    query=query, limit=25, sort=sort, year_min=year_min)
            except Exception as e:
                rows = []
                err = str(e)
            else:
                err = None
            GLib.idle_add(self._after_topic_search, rows, err)

        threading.Thread(target=_do, daemon=True).start()

    def _after_topic_search(self, rows, err):
        self._t_search_btn.set_sensitive(True)
        if err:
            self._t_status.set_markup(
                "<span foreground='#cc3333'>Search failed: {}</span>".format(
                    GLib.markup_escape_text(err)))
            return False
        if not rows:
            self._t_status.set_text("No results.")
            return False
        self._t_status.set_markup(
            "<small>{} result{}</small>".format(
                len(rows), "" if len(rows) == 1 else "s"))
        existing = self.parent_window._existing_dois_set()
        for r in rows:
            self._t_results_box.append(self._build_work_row(r, existing))
        return False

    # =========================================================
    # By title
    # =========================================================

    def _build_title_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._ti_query = Gtk.Entry()
        controls.append(_form_row("Title", self._ti_query))
        self._ti_query.set_placeholder_text(
            "e.g. AUSPEX graphical tool for X-ray diffraction "
            "(full or partial title)")

        opts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opts.append(Gtk.Label(label="Year ≥"))
        self._ti_year = Gtk.Entry()
        self._ti_year.set_max_length(4)
        self._ti_year.set_width_chars(5)
        self._ti_year.set_placeholder_text("(any)")
        opts.append(self._ti_year)
        opts.append(Gtk.Label(label="  Sort"))
        sl = Gtk.StringList()
        for label in ("Relevance", "Most cited", "Most recent"):
            sl.append(label)
        self._ti_sort = Gtk.DropDown(model=sl)
        # Title queries usually want the closest match by relevance,
        # not "most cited paper that mentions these words."
        self._ti_sort.set_selected(0)
        opts.append(self._ti_sort)
        controls.append(opts)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._ti_search_btn = Gtk.Button(label="Search OpenAlex")
        self._ti_search_btn.add_css_class("suggested-action")
        self._ti_search_btn.connect("clicked", self._on_title_search)
        btn_row.append(self._ti_search_btn)
        controls.append(btn_row)

        self._ti_query.connect("activate", self._on_title_search)

        box.append(controls)

        self._ti_status = Gtk.Label(xalign=0.0)
        box.append(self._ti_status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self._ti_results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scrolled.set_child(self._ti_results_box)
        box.append(scrolled)
        return box

    def _on_title_search(self, _btn):
        query = (self._ti_query.get_text() or "").strip()
        if not query:
            self._ti_status.set_text("Enter a title (or part of one).")
            return
        year_text = (self._ti_year.get_text() or "").strip()
        year_min = None
        if year_text:
            try:
                year_min = int(year_text)
            except ValueError:
                self._ti_status.set_text("Year must be a number.")
                return
        sort_idx = self._ti_sort.get_selected()
        sort = ("relevance", "cited", "recent")[max(0, min(sort_idx, 2))]

        self._ti_status.set_text("Searching OpenAlex…")
        self._ti_search_btn.set_sensitive(False)
        self._clear_box(self._ti_results_box)

        def _do():
            try:
                rows = metrics.search_works(
                    query=query, limit=20, sort=sort,
                    year_min=year_min, search_field="title")
            except Exception as e:
                rows = []
                err = str(e)
            else:
                err = None
            GLib.idle_add(self._after_title_search, rows, err)

        threading.Thread(target=_do, daemon=True).start()

    def _after_title_search(self, rows, err):
        self._ti_search_btn.set_sensitive(True)
        if err:
            self._ti_status.set_markup(
                "<span foreground='#cc3333'>Search failed: {}</span>".format(
                    GLib.markup_escape_text(err)))
            return False
        if not rows:
            self._ti_status.set_text("No results.")
            return False
        self._ti_status.set_markup(
            "<small>{} result{}</small>".format(
                len(rows), "" if len(rows) == 1 else "s"))
        existing = self.parent_window._existing_dois_set()
        for r in rows:
            self._ti_results_box.append(self._build_work_row(r, existing))
        return False

    # =========================================================
    # By PDB accession code
    # =========================================================

    def _build_pdb_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._pdb_query = Gtk.Entry()
        self._pdb_query.set_placeholder_text("e.g. 4hhb")
        self._pdb_query.set_max_length(4)
        self._pdb_query.set_width_chars(6)
        controls.append(_form_row("PDB code", self._pdb_query))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._pdb_search_btn = Gtk.Button(label="Search PDBe")
        self._pdb_search_btn.add_css_class("suggested-action")
        self._pdb_search_btn.connect("clicked", self._on_pdb_search)
        btn_row.append(self._pdb_search_btn)
        controls.append(btn_row)

        self._pdb_query.connect("activate", self._on_pdb_search)

        box.append(controls)

        self._pdb_status = Gtk.Label(xalign=0.0)
        box.append(self._pdb_status)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        self._pdb_results_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scrolled.set_child(self._pdb_results_box)
        box.append(scrolled)
        return box

    def _on_pdb_search(self, _btn):
        code = (self._pdb_query.get_text() or "").strip().lower()
        if not re.fullmatch(r"[0-9][a-z0-9]{3}", code):
            self._pdb_status.set_text(
                "Enter a 4-character PDB code (e.g. 4hhb).")
            return

        self._pdb_status.set_text("Looking up PDBe…")
        self._pdb_search_btn.set_sensitive(False)
        self._clear_box(self._pdb_results_box)

        def _do():
            err = None
            rows = []
            n_skipped = 0
            try:
                pubs = metrics.fetch_pdb_publications(code)
                for p in pubs:
                    if not p.get("doi"):
                        # Add path keys on DOI; show in the skip count.
                        n_skipped += 1
                        continue
                    # OpenAlex lookup gives citations/OA badge/auto-PDF
                    # on Add; fall back to the PDBe row if OpenAlex
                    # doesn't have this DOI.
                    oa = metrics.fetch_work_by_doi(p["doi"])
                    rows.append(oa or {
                        "doi": p["doi"],
                        "title": p["title"],
                        "year": p["year"],
                        "journal": p["journal"],
                        "first_author": p["first_author"],
                        "last_author": p["last_author"],
                        "authors": p["authors"],
                        "citations": 0,
                        "is_oa": False,
                        "oa_url": None,
                    })
            except Exception as e:
                err = str(e)
            GLib.idle_add(self._after_pdb_search, rows, n_skipped, err)

        threading.Thread(target=_do, daemon=True).start()

    def _after_pdb_search(self, rows, n_skipped, err):
        self._pdb_search_btn.set_sensitive(True)
        if err:
            self._pdb_status.set_markup(
                "<span foreground='#cc3333'>Lookup failed: {}</span>".format(
                    GLib.markup_escape_text(err)))
            return False
        if not rows and not n_skipped:
            self._pdb_status.set_text("No publications found for that code.")
            return False
        msg = "{} result{}".format(len(rows), "" if len(rows) == 1 else "s")
        if n_skipped:
            msg += " ({} skipped: no DOI)".format(n_skipped)
        self._pdb_status.set_markup("<small>{}</small>".format(msg))
        existing = self.parent_window._existing_dois_set()
        for r in rows:
            self._pdb_results_box.append(self._build_work_row(r, existing))
        return False

    # =========================================================
    # Shared work-row (topic + title results)
    # =========================================================

    def _build_work_row(self, r, existing_dois):
        """Reuse the parent window's `_build_related_row` for shape
        consistency, then prepend an "Add to library" button."""
        # The base row already has DOI button + in-library chip.
        base = self.parent_window._build_related_row(
            r, existing_dois, prefer_date=False, show_citations=True)
        # Extract its inner Box and append our Add button on the right.
        inner = base.get_child()   # Gtk.Box (info | right)
        # Stick an "Add" button at the very right.
        if not (r.get("doi") and r["doi"].lower() in existing_dois):
            add_btn = Gtk.Button(label="Add")
            add_btn.add_css_class("flat")
            add_btn.set_tooltip_text(
                "Import as a ghost (metadata only) — try Open Access PDF "
                "fetch separately if available.")
            add_btn.connect(
                "clicked", lambda _b, rr=r, btn=None: self._on_add_work(rr, _b))
            # Append to the right column (last child of inner).
            right = inner.get_last_child()
            right.append(add_btn)
        return base

    def _on_add_work(self, r, btn):
        """Build a BibTeX-shape dict from the OpenAlex work and route
        through the parent window's existing ghost-import path."""
        first = r.get("first_author") or ""
        title = (r.get("title") or "untitled").strip()
        first_word = (title.split() or ["paper"])[0].lower()
        # Sanitise into a portable bibtex key.
        import re
        safe = re.sub(r"[^A-Za-z0-9]", "", first.split()[-1] if first else "")
        key_year = str(r.get("year") or "")
        bibtex_key = (safe.lower() + key_year + first_word) or "openalex"
        br = {
            "title":   r.get("title"),
            "authors": list(r.get("authors") or []),
            "year":    r.get("year"),
            "journal": r.get("journal"),
            "doi":     r.get("doi"),
            "bibtex_key":  bibtex_key,
            "bibtex_type": "article",
            "bibtex_extra": {},
            "file": None,
        }
        also_get_pdf = bool(r.get("is_oa") and r.get("oa_url"))
        btn.set_sensitive(False)
        btn.set_label("Adding…")

        def on_done(success, message, label=None):
            # Keep the button disabled while the PDF fetch is still in
            # flight ("Fetching PDF…"); re-enabling only on a terminal
            # state avoids a double-add.
            terminal = label != "Fetching PDF…"
            btn.set_sensitive(terminal)
            btn.set_label(label or ("Added" if success else "Failed"))
            self._t_status.set_text(message)
        self.parent_window.add_reference_from_viewer(
            br, also_get_pdf, on_done)

    # =========================================================
    # Helpers
    # =========================================================

    def _clear_box(self, box):
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt


def _form_row(label, entry):
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    lbl = Gtk.Label(label=label)
    lbl.set_xalign(1.0)
    lbl.set_width_chars(11)
    row.append(lbl)
    entry.set_hexpand(True)
    row.append(entry)
    return row
