#!/usr/bin/env python3
"""Scan a directory tree of PDFs (or a single PDF); create sidecars +
thumbnails; populate the local SQLite index.

Usage:  pdforg-import.py [--refresh] <directory-or-pdf>

  --refresh   Re-extract metadata into existing sidecars (skipping ones
              flagged hand_edited=true). Preserves tags, notes, mark,
              and cached citation counts / authorships / abstract.

When the argument is a single .pdf file, only that file is processed
(useful for refreshing metadata on one paper at a time).
"""

import os
import sys

from . import importer, index


def main(argv):
    args = argv[1:]
    refresh = False
    if args and args[0] in ("--refresh", "-r"):
        refresh = True
        args = args[1:]
    if len(args) != 1:
        print("usage: {} [--refresh] <directory-or-pdf>".format(argv[0]))
        return 1
    target = args[0]
    if not os.path.exists(target):
        print("not found: {}".format(target))
        return 1
    conn = index.open_db()

    def progress(i, n, path, rec, status):
        if status == "duplicate" and rec:
            print("[{}/{}] DUPLICATE  {}\n           of  {}".format(
                i, n, path, rec.get("pdf_path", "?")))
            return
        if status == "renamed":
            print("[{}/{}] RENAMED    {}".format(i, n, path))
            return
        if status == "hand_edited":
            print("[{}/{}] HAND-EDITED (skipped) {}".format(i, n, path))
            return
        if status == "refreshed":
            tag = "REFRESH"
        else:
            tag = ""
        bits = []
        if rec:
            authors = rec.get("authors") or []
            if authors:
                if len(authors) > 2:
                    bits.append(", ".join(authors[:2]) + " et al.")
                else:
                    bits.append(", ".join(authors))
            if rec.get("year"):
                bits.append("({})".format(rec["year"]))
        suffix = "  -  " + " ".join(bits) if bits else ""
        print("[{}/{}] {}{}".format(i, n, path, suffix))

    if os.path.isfile(target) and target.lower().endswith(".pdf"):
        # Single-file mode.
        rec, status = (None, "error")
        try:
            if refresh:
                rec, status = importer.refresh_pdf(conn, target)
                if status == "no_sidecar":
                    rec, status = importer.import_pdf(conn, target)
            else:
                rec, status = importer.import_pdf(conn, target)
        except Exception as e:
            print("import failed for {}: {}".format(target, e))
        progress(1, 1, target, rec, status)
        print("{} 1 PDF".format("Refreshed" if refresh else "Imported"))
        return 0

    if not os.path.isdir(target):
        print("not a PDF or directory: {}".format(target))
        return 1

    n = importer.import_tree(conn, target, on_progress=progress, refresh=refresh)
    print("{} {} PDFs".format("Refreshed" if refresh else "Imported", n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
