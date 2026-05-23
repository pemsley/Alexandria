#!/usr/bin/env python3
"""Dev shim — runs the browser straight from the source tree.

After `pip install`, the console-script `alexandria-browse` does the same thing."""

import sys

from alexandria.browse import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
