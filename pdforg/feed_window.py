"""Subscription feed window: "follow this journal / save this topic
search and tell me what's new".

Single window with three regions:
  * **Subscriptions strip** at the top: list of current follows
    plus an "Add subscription…" button that drops down an inline
    form (journal name or ISSN, *or* free-text topic).
  * **Subscription filter** dropdown on the body header so the
    feed area can be restricted to one subscription at a time
    (default: All subscriptions).
  * **Feed body**: cards for `discovered` rows, sorted by
    `published_date` desc. Each card has title/journal/year/
    authors/abstract plus a "Get PDF" button that routes through
    `BrowserWindow.add_reference_from_viewer` so the existing
    ghost-import path is reused.

The background refresher (see `browse._feed_refresher`) writes
into `discovered`; this window only reads. A "Refresh now" button
forces a refresh of the current view via the same `feed.refresh_subscription`
the background thread uses.
"""

import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango

from . import feed, index


def open_window(parent, conn):
    """Show the feed window. Bound to its `parent` BrowserWindow
    because "Get PDF" needs to call back into the ghost-import
    entry point that lives on the browser."""
    win = FeedWindow(parent, conn)
    win.present()
    return win


def _clear_box(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


class FeedWindow(Adw.Window):
    def __init__(self, parent_window, conn):
        super().__init__()
        self.set_transient_for(parent_window)
        self.parent_window = parent_window
        self.conn = conn
        self.set_title("Subscriptions")
        self.set_default_size(820, 720)

        # `selected_sub_id` is None for "all subscriptions"; an
        # int for a specific one. Drives the body filter.
        self.selected_sub_id = None

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        outer.append(header)

        refresh_btn = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic")
        refresh_btn.set_tooltip_text(
            "Refresh selected subscription now")
        refresh_btn.connect("clicked", self._on_refresh_clicked)
        header.pack_end(refresh_btn)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_top(10)
        body.set_margin_bottom(10)

        body.append(self._build_subscriptions_strip())
        body.append(Gtk.Separator())
        body.append(self._build_feed_filter_row())

        self.feed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                spacing=8)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(self.feed_box)
        body.append(scrolled)

        outer.append(body)
        self.set_content(outer)

        self._refresh_subscriptions_strip()
        self._refresh_feed()

    # ──── Subscriptions strip ────────────────────────────────────

    def _build_subscriptions_strip(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                             spacing=6)
        title = Gtk.Label(xalign=0.0)
        title.set_markup(
            "<span size='small' alpha='75%'>Following</span>")
        title.set_hexpand(True)
        header_row.append(title)

        self._add_sub_button = Gtk.MenuButton()
        self._add_sub_button.set_label("Add…")
        self._add_sub_button.set_popover(self._build_add_popover())
        header_row.append(self._add_sub_button)
        box.append(header_row)

        # FlowBox of subscription pills. Each pill is a small
        # button with the name and a × remove-affordance. Clicking
        # the pill filters the feed below to that subscription;
        # clicking the × removes it after a confirm.
        self._subs_flow = Gtk.FlowBox()
        self._subs_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._subs_flow.set_max_children_per_line(6)
        self._subs_flow.set_row_spacing(4)
        self._subs_flow.set_column_spacing(4)
        box.append(self._subs_flow)

        return box

    def _build_add_popover(self):
        pop = Gtk.Popover()
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        b.set_margin_start(8)
        b.set_margin_end(8)
        b.set_margin_top(8)
        b.set_margin_bottom(8)

        # Mode chooser: Journal vs Topic. Radio-style toggles.
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=4)
        mode_row.add_css_class("linked")
        self._add_mode = "journal"
        self._add_journal_btn = Gtk.ToggleButton(label="Journal")
        self._add_topic_btn = Gtk.ToggleButton(label="Topic")
        self._add_journal_btn.set_active(True)
        self._add_topic_btn.set_group(self._add_journal_btn)
        self._add_journal_btn.connect(
            "toggled", self._on_add_mode_toggled, "journal")
        self._add_topic_btn.connect(
            "toggled", self._on_add_mode_toggled, "topic")
        mode_row.append(self._add_journal_btn)
        mode_row.append(self._add_topic_btn)
        b.append(mode_row)

        # Entry: journal-name lookup OR topic free-text.
        self._add_entry = Gtk.Entry()
        self._add_entry.set_placeholder_text(
            "Journal name or ISSN — e.g. Science or 0036-8075")
        self._add_entry.set_width_chars(34)
        self._add_entry.connect("activate", self._on_add_query)
        b.append(self._add_entry)

        # Action button — text changes per mode.
        self._add_action_btn = Gtk.Button(label="Find journal")
        self._add_action_btn.add_css_class("suggested-action")
        self._add_action_btn.connect("clicked", self._on_add_query)
        b.append(self._add_action_btn)

        # Results area (journal mode only — topic mode adds directly
        # without a picker step). Each row is clickable; clicking
        # adds the subscription.
        self._add_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=2)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(180)
        scrolled.set_min_content_width(440)
        scrolled.set_child(self._add_results)
        b.append(scrolled)

        self._add_status = Gtk.Label(xalign=0.0)
        self._add_status.set_markup(
            "<span size='small' alpha='65%'>Pick a journal from "
            "the list, or switch to Topic for a free-text "
            "OpenAlex search</span>")
        self._add_status.set_wrap(True)
        b.append(self._add_status)

        pop.set_child(b)
        return pop

    def _on_add_mode_toggled(self, btn, mode):
        if not btn.get_active():
            return
        self._add_mode = mode
        if mode == "journal":
            self._add_entry.set_placeholder_text(
                "Journal name or ISSN — e.g. Science or 0036-8075")
            self._add_action_btn.set_label("Find journal")
            self._add_status.set_markup(
                "<span size='small' alpha='65%'>Pick a journal from "
                "the list, or switch to Topic for a free-text "
                "OpenAlex search</span>")
        else:
            self._add_entry.set_placeholder_text(
                "Topic — e.g. T cells")
            self._add_action_btn.set_label("Add topic subscription")
            self._add_status.set_markup(
                "<span size='small' alpha='65%'>OpenAlex full-text "
                "search, sorted newest first, type:article|review|"
                "preprint</span>")
        _clear_box(self._add_results)

    def _on_add_query(self, _w):
        q = (self._add_entry.get_text() or "").strip()
        if not q:
            self._add_status.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "Type something first.</span>")
            return
        if self._add_mode == "topic":
            self._do_add_topic(q)
            return
        # Journal mode: kick off lookup in a thread, render results.
        self._add_action_btn.set_sensitive(False)
        self._add_status.set_markup(
            "<span size='small' alpha='75%'>Searching CrossRef…</span>")
        _clear_box(self._add_results)
        threading.Thread(
            target=self._do_journal_lookup,
            args=(q,), daemon=True).start()

    def _do_journal_lookup(self, q):
        try:
            hits = feed.find_journal_by_name(q)
        except Exception as e:
            GLib.idle_add(self._after_journal_lookup, [], str(e))
            return
        GLib.idle_add(self._after_journal_lookup, hits, None)

    def _after_journal_lookup(self, hits, err):
        self._add_action_btn.set_sensitive(True)
        if err:
            self._add_status.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "Lookup failed: {}</span>".format(
                    GLib.markup_escape_text(err)))
            return False
        if not hits:
            self._add_status.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "No journals found.</span>")
            return False
        self._add_status.set_markup(
            "<span size='small' alpha='65%'>{} match{} — click "
            "one to follow</span>".format(
                len(hits), "" if len(hits) == 1 else "es"))
        for h in hits[:30]:
            self._add_results.append(self._build_journal_pick_row(h))
        return False

    def _build_journal_pick_row(self, h):
        btn = Gtk.Button()
        btn.add_css_class("flat")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(xalign=0.0)
        title.set_markup("<b>{}</b>".format(
            GLib.markup_escape_text(h.get("title") or "(untitled)")))
        title.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title)
        meta = Gtk.Label(xalign=0.0)
        meta.set_markup(
            "<span size='small' alpha='65%'>ISSN: {} · {}</span>".format(
                GLib.markup_escape_text(", ".join(h.get("issns") or [])),
                GLib.markup_escape_text(h.get("publisher") or "")))
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(meta)
        btn.set_child(box)
        btn.connect("clicked",
                    lambda _b, h=h: self._do_add_journal(h))
        return btn

    def _do_add_journal(self, h):
        name = h.get("title") or "Untitled journal"
        issns = ",".join(h.get("issns") or [])
        if not issns:
            self._add_status.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "That journal has no ISSN — cannot follow.</span>")
            return
        try:
            sid = index.add_subscription(
                self.conn, "journal_issn", name, issns)
        except Exception as e:
            self._add_status.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "Could not add: {}</span>".format(
                    GLib.markup_escape_text(str(e))))
            return
        self._add_status.set_markup(
            "<span size='small'>Following <b>{}</b>. Fetching "
            "first batch…</span>".format(
                GLib.markup_escape_text(name)))
        self._add_entry.set_text("")
        _clear_box(self._add_results)
        self._refresh_subscriptions_strip()
        # Fire a one-shot refresh of the new subscription so the
        # user sees results immediately instead of waiting on the
        # background timer.
        threading.Thread(target=self._initial_fetch,
                         args=(sid,), daemon=True).start()
        # Close the popover; the strip + body update on the next
        # idle cycle.
        self._add_sub_button.get_popover().popdown()

    def _do_add_topic(self, query):
        # Topic subscriptions just store the query string; the
        # fetcher hits OpenAlex with it.
        try:
            sid = index.add_subscription(
                self.conn, "openalex_query", query, query)
        except Exception as e:
            self._add_status.set_markup(
                "<span size='small' foreground='#cc3333'>"
                "Could not add: {}</span>".format(
                    GLib.markup_escape_text(str(e))))
            return
        self._add_status.set_markup(
            "<span size='small'>Following topic <b>{}</b>. "
            "Fetching first batch…</span>".format(
                GLib.markup_escape_text(query)))
        self._add_entry.set_text("")
        self._refresh_subscriptions_strip()
        threading.Thread(target=self._initial_fetch,
                         args=(sid,), daemon=True).start()
        self._add_sub_button.get_popover().popdown()

    def _initial_fetch(self, subscription_id):
        # Look the row back up so we have all the fields the
        # refresh_subscription helper expects.
        rows = [s for s in index.list_subscriptions(self.conn)
                if s["id"] == subscription_id]
        if not rows:
            return
        sub = rows[0]
        try:
            fetched, new = feed.refresh_subscription(self.conn, sub)
            index.mark_subscription_fetched(self.conn, sub["id"])
        except Exception as e:
            print("[feed] initial fetch failed for {}: {}"
                  .format(sub["name"], e))
            return
        GLib.idle_add(self._after_initial_fetch,
                      subscription_id, fetched, new)

    def _after_initial_fetch(self, subscription_id, fetched, new):
        self.selected_sub_id = subscription_id
        self._refresh_subscriptions_strip()
        self._refresh_feed()
        return False

    def _refresh_subscriptions_strip(self):
        _clear_box(self._subs_flow)
        subs = index.list_subscriptions(self.conn)

        # "All" pill (selected by default).
        all_pill = self._build_sub_pill(
            None, "All subscriptions",
            self.selected_sub_id is None, removable=False)
        self._subs_flow.append(all_pill)

        for s in subs:
            kind_glyph = "[J]" if s["kind"] == "journal_issn" else "[T]"
            label = "{} {}".format(kind_glyph, s["name"])
            pill = self._build_sub_pill(
                s["id"], label,
                self.selected_sub_id == s["id"],
                removable=True)
            self._subs_flow.append(pill)

        # Rebuild the body filter dropdown too.
        self._rebuild_filter_dropdown(subs)

    def _build_sub_pill(self, sub_id, label, selected, removable):
        # FlowBoxChild wrapping a horizontal box of (select-button,
        # remove-button).
        wrapper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          spacing=0)
        sel_btn = Gtk.Button(label=label)
        sel_btn.add_css_class("flat" if not selected else "suggested-action")
        sel_btn.connect("clicked",
                        lambda _b, sid=sub_id: self._on_pill_click(sid))
        wrapper.append(sel_btn)
        if removable:
            rm = Gtk.Button.new_from_icon_name("window-close-symbolic")
            rm.add_css_class("flat")
            rm.set_tooltip_text("Stop following")
            rm.connect(
                "clicked",
                lambda _b, sid=sub_id, name=label:
                    self._on_pill_remove(sid, name))
            wrapper.append(rm)
        return wrapper

    def _on_pill_click(self, sub_id):
        self.selected_sub_id = sub_id
        self._refresh_subscriptions_strip()
        self._refresh_feed()

    def _on_pill_remove(self, sub_id, label):
        dlg = Gtk.AlertDialog()
        dlg.set_modal(True)
        dlg.set_message("Stop following?")
        dlg.set_detail(
            "This removes the subscription and any unimported "
            "discovered articles for it.\n\nLibrary papers you've "
            "already imported are unaffected.")
        dlg.set_buttons(["Cancel", "Remove"])
        dlg.set_default_button(0)
        dlg.set_cancel_button(0)

        def on_choice(_d, result):
            try:
                idx_chosen = dlg.choose_finish(result)
            except GLib.Error:
                return
            if idx_chosen != 1:
                return
            try:
                index.remove_subscription(self.conn, sub_id)
            except Exception as e:
                print("remove subscription failed:", e)
                return
            if self.selected_sub_id == sub_id:
                self.selected_sub_id = None
            self._refresh_subscriptions_strip()
            self._refresh_feed()

        dlg.choose(self, None, on_choice)

    # ──── Feed body filter ───────────────────────────────────────

    def _build_feed_filter_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                      spacing=8)
        self._filter_label = Gtk.Label(xalign=0.0)
        self._filter_label.set_markup(
            "<span size='small' alpha='65%'>Recent articles</span>")
        self._filter_label.set_hexpand(True)
        row.append(self._filter_label)
        return row

    def _rebuild_filter_dropdown(self, subs):
        # Body filter is implicit via the pill selection above;
        # the label here just echoes the current selection for
        # clarity. (No dropdown — keeps the surface minimal.)
        if self.selected_sub_id is None:
            text = "Recent articles · all subscriptions"
        else:
            match = [s for s in subs if s["id"] == self.selected_sub_id]
            name = match[0]["name"] if match else "(unknown)"
            text = "Recent articles · {}".format(name)
        self._filter_label.set_markup(
            "<span size='small' alpha='65%'>{}</span>".format(
                GLib.markup_escape_text(text)))

    # ──── Feed body ──────────────────────────────────────────────

    def _refresh_feed(self):
        _clear_box(self.feed_box)
        subs = index.list_subscriptions(self.conn)
        if not subs:
            empty = Gtk.Label(xalign=0.5)
            empty.set_markup(
                "<span alpha='65%'>No subscriptions yet. Click "
                "“Add…” to follow a journal or topic.</span>")
            empty.set_margin_top(40)
            self.feed_box.append(empty)
            return
        # Gather rows: if selected, just that subscription;
        # otherwise the union, tagged by subscription name.
        target_subs = (
            [s for s in subs if s["id"] == self.selected_sub_id]
            if self.selected_sub_id is not None else subs)
        rows = []
        for s in target_subs:
            for d in index.discovered_for(self.conn, s["id"], limit=100):
                rows.append((s, d))
        # Sort union by published_date desc; rows without dates
        # fall to the end.
        rows.sort(
            key=lambda sr: (sr[1].get("published_date") or "",
                            sr[1].get("fetched_at") or ""),
            reverse=True)
        if not rows:
            empty = Gtk.Label(xalign=0.5)
            empty.set_markup(
                "<span alpha='65%'>No articles fetched yet.</span>")
            empty.set_margin_top(40)
            self.feed_box.append(empty)
            return
        for sub, art in rows[:100]:
            self.feed_box.append(self._build_feed_card(sub, art))

    def _build_feed_card(self, sub, art):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_margin_top(6)
        outer.set_margin_bottom(6)

        title = Gtk.Label(xalign=0.0)
        t = art.get("title") or "(untitled)"
        title.set_markup("<b>{}</b>".format(
            GLib.markup_escape_text(t)))
        title.set_wrap(True)
        title.set_xalign(0.0)
        outer.append(title)

        meta_bits = []
        if art.get("journal"):
            meta_bits.append(art["journal"])
        if art.get("published_date"):
            meta_bits.append(art["published_date"])
        elif art.get("year"):
            meta_bits.append(str(art["year"]))
        meta_bits.append("via " + (sub["name"] or ""))
        meta = Gtk.Label(xalign=0.0)
        meta.set_markup(
            "<span size='small' alpha='65%'>{}</span>".format(
                GLib.markup_escape_text("  ·  ".join(meta_bits))))
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        outer.append(meta)

        # Authors line, truncated.
        try:
            import json
            authors = (json.loads(art["authors_json"])
                       if art.get("authors_json") else [])
        except Exception:
            authors = []
        if authors:
            short = ", ".join(authors[:6])
            if len(authors) > 6:
                short += ", …"
            au = Gtk.Label(xalign=0.0)
            au.set_markup(
                "<span size='small'>{}</span>".format(
                    GLib.markup_escape_text(short)))
            au.set_ellipsize(Pango.EllipsizeMode.END)
            outer.append(au)

        if art.get("abstract"):
            abst = Gtk.Label(xalign=0.0)
            text = art["abstract"]
            if len(text) > 360:
                text = text[:357] + "…"
            abst.set_markup(
                "<span size='small' alpha='80%'>{}</span>".format(
                    GLib.markup_escape_text(text)))
            abst.set_wrap(True)
            abst.set_xalign(0.0)
            outer.append(abst)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                          spacing=4)
        btn_row.set_halign(Gtk.Align.END)
        if art.get("doi"):
            add_btn = Gtk.Button(label="Add")
            add_btn.add_css_class("flat")
            # Matches the Discover dialog's per-row vocabulary.
            # Always imports as a ghost; for OA-flagged rows we
            # also try the PDF download, otherwise we stop there
            # — the previous "Get PDF" label promised more than
            # was honest for the Nature news / CrossRef-sourced
            # rows where there's no OA copy.
            add_btn.set_tooltip_text(
                "Add this article to your library (metadata only). "
                "If an Open Access PDF is known, also download it.")
            add_btn.connect("clicked",
                            lambda _b, a=art: self._on_add(a))
            btn_row.append(add_btn)
        outer.append(btn_row)

        frame = Gtk.Frame()
        frame.set_child(outer)
        return frame

    def _on_add(self, art):
        """Import the article into the library as a ghost via the
        BrowserWindow's existing add path. We only ask the path
        to chase the PDF when we have a positive OA signal —
        matching `discover.py`'s per-row "Add" semantics. For
        CrossRef-sourced rows OA is always False (CrossRef doesn't
        carry the flag), so journal-subscription clicks just add
        the ghost; the user can press "Get PDF" on the resulting
        ghost card if they want to try the OA chase explicitly.
        That avoids the surprise-browser-tab fallback for news
        articles, which is the case that motivated the rename."""
        try:
            import json
            authors = (json.loads(art["authors_json"])
                       if art.get("authors_json") else [])
        except Exception:
            authors = []
        br = {
            "title":   art.get("title"),
            "authors": authors,
            "year":    art.get("year"),
            "journal": art.get("journal"),
            "doi":     art.get("doi"),
            "bibtex_key": _suggest_bibtex_key(authors, art.get("year"),
                                              art.get("title")),
        }
        also_get_pdf = bool(art.get("is_oa") and art.get("oa_url"))

        def on_done(success, message):
            # No toast surface in this window; let the message
            # land on the parent's status if available.
            try:
                self.parent_window._toast(message, timeout=5)
            except Exception:
                pass

        self.parent_window.add_reference_from_viewer(
            br, also_get_pdf=also_get_pdf, on_done=on_done)

    # ──── Refresh button ─────────────────────────────────────────

    def _on_refresh_clicked(self, _btn):
        # Refresh only the currently-selected subscription (or
        # nothing when "All" is selected — that's the background
        # thread's job).
        if self.selected_sub_id is None:
            try:
                self.parent_window._toast(
                    "Select a subscription pill to force a refresh "
                    "of just that one.", timeout=5)
            except Exception:
                pass
            return
        threading.Thread(target=self._initial_fetch,
                         args=(self.selected_sub_id,), daemon=True).start()


def _suggest_bibtex_key(authors, year, title):
    """Produce a plausible bibtex_key for a discovered article so
    `bibtex_import.import_record` has something stable to key on.
    Pattern: <FirstSurname><Year><FirstWord>. Lowercased.

    Doesn't need to be unique here — `bibtex_import` disambiguates
    further if a collision shows up at import time."""
    surname = ""
    if authors:
        first = authors[0] or ""
        parts = first.split()
        surname = parts[-1] if parts else ""
    surname = "".join(c for c in surname if c.isalnum()) or "anon"
    y = str(year) if year else "nd"
    word = ""
    if title:
        for w in title.split():
            cleaned = "".join(c for c in w if c.isalnum())
            if len(cleaned) >= 3:
                word = cleaned.lower()
                break
    return "{}{}{}".format(surname.lower(), y, word) or "ref"
