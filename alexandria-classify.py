#!/usr/bin/env python3
"""Dev shim — runs the classifier straight from the source tree.

After `pip install`, the console-script `alexandria-classify` does the same thing."""

import sys

from alexandria.classify import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
