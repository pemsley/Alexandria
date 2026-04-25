"""GFileMonitor-driven library watcher.

v1: monitors the LIBRARY_ROOT directory (flat — no subdirs) for *.pdf
file events. CREATED / CHANGES_DONE_HINT / MOVED_IN → import the file
in a background thread. DELETED / MOVED_OUT → drop the index row.
RENAMED in-place → re-import the new name; SHA-256 detection in
import_pdf adopts the existing record.

External edits to .meta.json or thumbnail PNGs are ignored (filtered
by file extension), so our own writes don't echo back as events.
"""

import os
import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

from . import importer


# How long to wait between batched events from the OS. Lets a
# `cp ~/Desktop/*.pdf ~/pdfs/` of 50 PDFs collapse into a few events
# rather than 50 simultaneous import threads.
RATE_LIMIT_MS = 1500


def _is_pdf(path):
    return bool(path) and path.lower().endswith(".pdf")


class LibraryWatcher:
    """Watches `library_root` for PDF changes and keeps the SQLite index
    in sync. on_change(status_str) is called on the GLib main thread
    after each successful change."""

    def __init__(self, conn, library_root, on_change_cb=None):
        self.conn = conn
        self.root = library_root
        self.on_change = on_change_cb
        self.monitor = None
        self._reconcile_thread = None

    # --- Lifecycle ----------------------------------------------------

    def start(self):
        if self.monitor:
            return
        if not os.path.isdir(self.root):
            return
        try:
            gfile = Gio.File.new_for_path(self.root)
            self.monitor = gfile.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error as e:
            print("LibraryWatcher: monitor_directory failed:", e)
            self.monitor = None
            return
        self.monitor.set_rate_limit(RATE_LIMIT_MS)
        self.monitor.connect("changed", self._on_changed)

    def stop(self):
        if self.monitor:
            self.monitor.cancel()
            self.monitor = None

    def reconcile_startup(self):
        """Catch up on PDFs added while the browser was closed.
        Doesn't auto-delete missing entries (a temporarily unmounted
        share would otherwise wipe the index)."""
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return
        if not os.path.isdir(self.root):
            return
        self._reconcile_thread = threading.Thread(
            target=self._do_reconcile, daemon=True)
        self._reconcile_thread.start()

    def _do_reconcile(self):
        try:
            importer.import_tree(self.conn, self.root)
        except Exception as e:
            print("LibraryWatcher: reconcile failed:", e)
            return
        if self.on_change:
            GLib.idle_add(self.on_change, "reconcile")

    # --- Event handling -----------------------------------------------

    def _on_changed(self, _monitor, gfile, other_file, event_type):
        path = gfile.get_path() if gfile else None
        other = other_file.get_path() if other_file else None

        et = event_type
        if et in (Gio.FileMonitorEvent.CREATED,
                  Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                  Gio.FileMonitorEvent.MOVED_IN):
            if _is_pdf(path):
                self._spawn(self._do_import, path)

        elif et in (Gio.FileMonitorEvent.DELETED,
                    Gio.FileMonitorEvent.MOVED_OUT):
            if _is_pdf(path):
                self._spawn(self._do_delete, path)

        elif et == Gio.FileMonitorEvent.RENAMED:
            # Re-import at the new path; import_pdf's SHA-256 detection
            # adopts the existing index row.
            if _is_pdf(other):
                self._spawn(self._do_import, other)
            elif _is_pdf(path):
                # Renamed to non-PDF (e.g. ".pdf.bak") — drop it.
                self._spawn(self._do_delete, path)

    def _spawn(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _do_import(self, path):
        try:
            _rec, status = importer.import_pdf(self.conn, path)
        except Exception as e:
            print("watcher: import failed for {}: {}".format(path, e))
            return
        # "recent" is the no-op self-event case; don't bother the UI.
        if status in ("recent",):
            return
        if self.on_change:
            GLib.idle_add(self.on_change, status)

    def _do_delete(self, path):
        try:
            importer.delete_pdf(self.conn, path)
        except Exception as e:
            print("watcher: delete failed for {}: {}".format(path, e))
            return
        if self.on_change:
            GLib.idle_add(self.on_change, "deleted")
