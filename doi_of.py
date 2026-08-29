#!/usr/bin/env python3
"""Print the DOI (and title) Alexandria extracts from a PDF — the same
extraction the importer/refresh uses, run straight from the repo code so
you can test it without the GUI or the library database.

Usage:
    python3 doi_of.py <file.pdf> [<file.pdf> ...]

Run it with the full-featured interpreter that has pypdf/pdftotext, e.g.
    /home/paule/autobuild/build-for-chapi-arch/bin/python3 doi_of.py foo.pdf
"""

import sys

from alexandria import extract


def main(argv):
    if len(argv) < 2:
        sys.exit("usage: {} <file.pdf> [<file.pdf> ...]".format(argv[0]))
    for pdf in argv[1:]:
        try:
            rec = extract.extract_from_pdf(pdf)
        except Exception as e:
            print("{}\tERROR: {}".format(pdf, e))
            continue
        print("{}\t{}".format(rec.get("doi") or "(no DOI)", pdf))


if __name__ == "__main__":
    main(sys.argv)
