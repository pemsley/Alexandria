"""Pure coalescing logic for import-start toasts.

Kept GTK-free so it can be unit tested without a display. The browser
window owns the rolling-window state and the Adw.Toast objects; this
module only decides *what* to show given the names seen so far in the
current window.
"""

# Number of near-simultaneous import starts at which we stop naming each
# file and collapse to a single "Importing N PDFs…" toast.
COLLAPSE_THRESHOLD = 3


def toast_action(window_names):
    """Decide the toast to show for the current import window.

    `window_names` is the list of basenames whose imports have started
    within the current rolling window, oldest first, newest last.

    Returns one of:
      ("name", basename) — show/keep a per-file "Importing <name>…" toast
      ("count", n)       — show/update one "Importing n PDFs…" toast
      ("noop", None)     — nothing to show (empty window)
    """
    n = len(window_names)
    if n == 0:
        return ("noop", None)
    if n < COLLAPSE_THRESHOLD:
        return ("name", window_names[-1])
    return ("count", n)
