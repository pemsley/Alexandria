#!/usr/bin/env python3
"""Scan a directory tree of PDFs; create sidecars + thumbnails;
populate the local SQLite index.

Usage:  pdforg-import.py [--refresh] <directory>

  --refresh   Re-extract metadata into existing sidecars (skipping ones
              flagged hand_edited=true). Preserves tags, notes, and
              cached citation counts.
"""

import sys

from . import importer, index


def main(argv):
    args = argv[1:]
    refresh = False
    if args and args[0] in ("--refresh", "-r"):
        refresh = True
        args = args[1:]
    if len(args) != 1:
        print("usage: {} [--refresh] <pdf-directory>".format(argv[0]))
        return 1
    root = args[0]
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

    n = importer.import_tree(conn, root, on_progress=progress, refresh=refresh)
    print("{} {} PDFs".format("Refreshed" if refresh else "Imported", n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
