#!/usr/bin/env python3
"""Walk a directory tree of PDFs and classify each as PAPER, NOT_PAPER,
or UNCERTAIN. No sidecars are written. Progress goes to stderr; the
final summary to stdout, so you can redirect it to a file.

Usage:  alexandria-classify.py <directory>  [> report.txt]
"""

import os
import re
import sys

from . import extract

_ARXIV_RE = re.compile(r"\barxiv:\s*\d{4}\.\d{4,5}\b", re.IGNORECASE)


def _has_paper_text(text):
    if not text:
        return False
    low = text.lower()
    if "abstract" in low and ("references" in low or "introduction" in low):
        return True
    return False


def classify(pdf_path):
    """Return (label, rec) where label is 'PAPER' / 'NOT_PAPER' / 'UNCERTAIN'."""
    rec = extract.extract_from_pdf(pdf_path)
    text = extract._first_page_text(pdf_path)

    has_doi = bool(rec.get("doi"))
    has_arxiv = bool(text and _ARXIV_RE.search(text))
    has_paper_struct = _has_paper_text(text)
    has_title = bool(rec.get("title"))
    n_authors = len(rec.get("authors") or [])

    paper = (has_doi
             or has_arxiv
             or has_paper_struct
             or (has_title and n_authors >= 2))
    if paper:
        return "PAPER", rec

    nothing = (not has_title and n_authors == 0 and not has_doi
               and not (text and "abstract" in text.lower()))
    if nothing:
        return "NOT_PAPER", rec

    return "UNCERTAIN", rec


def _fmt_rec(rec):
    if not rec:
        return ""
    bits = []
    a = rec.get("authors") or []
    if a:
        if len(a) > 2:
            bits.append(", ".join(a[:2]) + " et al.")
        else:
            bits.append(", ".join(a))
    if rec.get("year"):
        bits.append("({})".format(rec["year"]))
    if rec.get("doi"):
        bits.append(rec["doi"])
    if rec.get("title") and not bits:
        t = rec["title"]
        if len(t) > 80:
            t = t[:77] + "..."
        bits.append("“" + t + "”")
    return "  ".join(bits)


def main(argv=None):
    if argv is None:
        argv = sys.argv

    if len(argv) != 2:
        print("usage: {} <directory>".format(argv[0]), file=sys.stderr)
        return 1
    root = argv[1]
    if not os.path.isdir(root):
        print("not a directory: {}".format(root), file=sys.stderr)
        return 1

    pdfs = []
    for dp, _d, files in os.walk(root):
        for n in files:
            if n.lower().endswith(".pdf"):
                pdfs.append(os.path.join(dp, n))
    pdfs.sort()
    n = len(pdfs)
    if n == 0:
        print("no PDFs under {}".format(root), file=sys.stderr)
        return 0

    buckets = {"PAPER": [], "NOT_PAPER": [], "UNCERTAIN": []}
    for i, p in enumerate(pdfs, 1):
        try:
            label, rec = classify(p)
        except Exception as e:
            print("classify failed for {}: {}".format(p, e), file=sys.stderr)
            label, rec = "UNCERTAIN", None
        buckets[label].append((p, rec))
        print("[{}/{}] {:<10}  {}".format(i, n, label, p), file=sys.stderr)

    for label in ["PAPER", "NOT_PAPER", "UNCERTAIN"]:
        items = buckets[label]
        pretty = label.replace("_", " ")
        print()
        print("=== {} ({}) ===".format(pretty, len(items)))
        for path, rec in items:
            extra = _fmt_rec(rec)
            print("  {}{}".format(path, "  -  " + extra if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
