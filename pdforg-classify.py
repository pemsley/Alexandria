#!/usr/bin/env python3
"""Dev shim — runs the classifier straight from the source tree.

After `pip install`, the console-script `pdforg-classify` does the same thing."""

import sys

from pdforg.classify import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
