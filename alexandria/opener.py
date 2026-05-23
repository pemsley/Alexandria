"""Cross-platform "open this URL or file path in the user's default app".

xdg-open is Linux-only. macOS has `open`, Windows has `os.startfile`.
For URLs, the stdlib's webbrowser.open() abstracts the difference; for
local file paths we shell out per-platform.
"""

import os
import subprocess
import sys
import webbrowser


def open_external(target):
    """Open a URL or local file path in the user's default application.

    Best-effort: returns True on success, False if no opener was
    available. Never raises.
    """
    if not target:
        return False

    if "://" in target:
        try:
            return bool(webbrowser.open(target))
        except Exception:
            return False

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", target],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            os.startfile(target)
        else:
            subprocess.Popen(["xdg-open", target],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError):
        return False
