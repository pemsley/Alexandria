#!/usr/bin/env python3
"""Dev shim — runs the importer straight from the source tree.

After `pip install`, the console-script `pdforg-import` does the same thing."""

import sys

from pdforg.cli_import import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
