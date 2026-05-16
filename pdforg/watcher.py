"""GFileMonitor-driven library watcher.

Monitors the LIBRARY_ROOT directory (flat — no subdirs) for two kinds
of events:

* PDF files: CREATED / CHANGES_DONE_HINT / MOVED_IN → import in a
  background thread. DELETED / MOVED_OUT → drop the index row.
  RENAMED in-place → re-import; SHA-256 detection adopts the row.

* Sidecar files (`*.alexandria`): CHANGED / CHANGES_DONE_HINT /
  CREATED → re-read the sidecar and refresh the index row. This is
  what makes `pdforg-import --refresh` invisibly update the running
  browser — the CLI rewrites the JSON, the watcher sees it, the row
  is upserted, the GUI redraws.
"""

import os
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

from . import bibtex_import, importer, index, sidecar


# How long to wait between batched events from the OS. Lets a
# `cp ~/Desktop/*.pdf ~/pdfs/` of 50 PDFs collapse into a few events
# rather than 50 simultaneous import threads.
RATE_LIMIT_MS = 1500


def _is_pdf(path):
    return bool(path) and path.lower().endswith(".pdf")


def _is_sidecar(path):
    return bool(path) and path.endswith(sidecar.SIDECAR_SUFFIX)


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
        # abspath -> expiry timestamp. Events on suppressed paths are
        # dropped — used during ghost-merge so the file we're about to
        # copy / re-import / roll back doesn't trigger nested watcher
        # imports.
        self._suppress = {}
        self._suppress_lock = threading.Lock()

    def suppress(self, path, secs):
        """Public: silence watcher events on `path` for `secs` seconds.
        Used by callers that write to a sidecar themselves and don't
        want the resulting CHANGED event to drive a card-list reload
        (e.g. caching cited-by / references lists)."""
        self._suppress_path(path, secs)

    def _suppress_path(self, path, secs):
        if not path:
            return
        with self._suppress_lock:
            self._suppress[os.path.abspath(path)] = time.time() + secs

    def _is_suppressed(self, path):
        if not path:
            return False
        ap = os.path.abspath(path)
        with self._suppress_lock:
            exp = self._suppress.get(ap)
            if exp is None:
                return False
            if exp < time.time():
                self._suppress.pop(ap, None)
                return False
            return True

    # --- Lifecycle ----------------------------------------------------

    def start(self):
        if self.monitor:
            print("[watcher] start: already running")
            return
        if not os.path.isdir(self.root):
            print("[watcher] start: root is not a directory: {!r}".format(self.root))
            return
        try:
            gfile = Gio.File.new_for_path(self.root)
            self.monitor = gfile.monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error as e:
            print("[watcher] monitor_directory failed:", e)
            self.monitor = None
            return
        self.monitor.set_rate_limit(RATE_LIMIT_MS)
        self.monitor.connect("changed", self._on_changed)
        print("[watcher] watching {} (rate-limit {}ms)".format(
            self.root, RATE_LIMIT_MS))

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
        et_name = getattr(et, "value_nick", str(et))
        print("[watcher] event={} path={} other={}".format(
            et_name, path, other))
        if self._is_suppressed(path) or self._is_suppressed(other):
            print("[watcher] suppressed (in-flight ghost-merge)")
            return
        if et in (Gio.FileMonitorEvent.CREATED,
                  Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                  Gio.FileMonitorEvent.MOVED_IN):
            if _is_pdf(path):
                self._spawn(self._do_import, path)
            elif _is_sidecar(path):
                self._spawn(self._do_resync_sidecar, path)

        elif et == Gio.FileMonitorEvent.CHANGED:
            # PDFs aren't usually edited in place; sidecars are
            # (e.g. by `pdforg-import --refresh` or hand-edits).
            if _is_sidecar(path):
                self._spawn(self._do_resync_sidecar, path)

        elif et in (Gio.FileMonitorEvent.DELETED,
                    Gio.FileMonitorEvent.MOVED_OUT):
            if _is_pdf(path):
                self._spawn(self._do_delete, path)
            # Sidecar deletions are ignored: most are our own atomic-
            # write temporaries; a real sidecar removal will resolve
            # next time the PDF is imported.

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
        print("[watcher] import start: {}".format(path))
        try:
            rec, status = importer.import_pdf(self.conn, path)
        except Exception as e:
            print("[watcher] import failed for {}: {}".format(path, e))
            return

        # Ghost-merge: the dropped PDF's DOI matched a BibTeX-only stub.
        # Route through attach_pdf_to_ghost so the ghost's curation is
        # merged onto the new sidecar and the ghost row is removed.
        if (status == "duplicate" and rec
                and sidecar.is_ghost_path(rec.get("pdf_path") or "")):
            print("[watcher] duplicate is a ghost ({}); attaching"
                  .format(rec.get("pdf_path")))
            # Predict the path attach_pdf_to_ghost will copy to and
            # suppress watcher events on it for the duration. Without
            # this the create/changed events on the new file race with
            # attach's own import_pdf call, which then sees its own
            # output as a duplicate and rolls back the copy.
            ghost_pdf = rec.get("pdf_path") or ""
            ghost_key = (ghost_pdf[len(sidecar.GHOST_PATH_PREFIX):]
                         if ghost_pdf.startswith(sidecar.GHOST_PATH_PREFIX)
                         else "")
            try:
                predicted = bibtex_import._unique_target_path(
                    self.root, ghost_key) if ghost_key else None
            except Exception:
                predicted = None
            if predicted:
                self._suppress_path(predicted, 30)
                print("[watcher] suppressing events on {} for 30s"
                      .format(predicted))
            try:
                new_path, gstatus, gmsg = bibtex_import.attach_pdf_to_ghost(
                    self.conn, dict(rec), path, self.root)
            except Exception as e:
                print("[watcher] ghost-merge failed for {}: {}".format(path, e))
                return
            if new_path:
                # Lift suppression early on success so legitimate
                # follow-up edits aren't dropped.
                with self._suppress_lock:
                    self._suppress.pop(os.path.abspath(new_path), None)
            print("[watcher] ghost-merge: {} -> {} ({})".format(
                path, gstatus, gmsg))
            # attach_pdf_to_ghost copies source → <bibtex_key>.pdf. If
            # source was already in the library root, drop it so we
            # don't leave two copies of the same PDF on disk.
            if (gstatus == "merged" and new_path
                    and os.path.abspath(new_path) != os.path.abspath(path)
                    and os.path.isfile(path)):
                try:
                    os.remove(path)
                    print("[watcher] removed duplicate source: {}".format(path))
                except OSError as e:
                    print("[watcher] could not remove source {}: {}"
                          .format(path, e))
            if self.on_change:
                GLib.idle_add(self.on_change, gstatus)
            return

        if status == "duplicate" and rec:
            print("[watcher] import done: {} -> duplicate of {}".format(
                path, rec.get("pdf_path") or "?"))
        else:
            print("[watcher] import done: {} -> {}".format(path, status))
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

    def _do_resync_sidecar(self, sc_path):
        """A sidecar was written externally (e.g. by `--refresh`).
        Re-read it and update the index row from the new contents."""
        if not os.path.isfile(sc_path):
            return
        suffix = sidecar.SIDECAR_SUFFIX
        if not sc_path.endswith(suffix):
            return
        pdf_path = sc_path[:-len(suffix)]
        if not os.path.isfile(pdf_path):
            return
        try:
            rec = sidecar.read(sc_path)
        except Exception as e:
            print("watcher: sidecar read failed for {}: {}".format(sc_path, e))
            return
        th_path = sidecar.thumb_path_for(pdf_path)
        try:
            mtime = os.path.getmtime(sc_path)
            index.upsert(self.conn, pdf_path, sc_path,
                         th_path if os.path.isfile(th_path) else None,
                         rec, mtime)
        except Exception as e:
            print("watcher: index upsert failed for {}: {}".format(sc_path, e))
            return
        if self.on_change:
            GLib.idle_add(self.on_change, "sidecar")
