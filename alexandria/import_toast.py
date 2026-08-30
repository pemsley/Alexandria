"""Pure coalescing logic for import-start toasts.

Kept GTK-free so it can be unit tested without a display. The browser
window owns the rolling-window state and the Adw.Toast objects; this
module only decides *what* to show given the names seen so far in the
current window.
"""

# Number of near-simultaneous import starts at which we stop naming each
# file and collapse to a single "Importing N PDFs…" toast.
COLLAPSE_THRESHOLD = 3


def record_start(window_names, basename):
    """Note that `basename` has begun importing, returning
    `(names, is_new)`.

    A single dropped PDF reaches import_pdf more than once — the
    drop handler imports it, and the watcher fires again for the
    file's CREATED and CHANGES_DONE_HINT events — so the same name
    arrives repeatedly within one window. Each repeat used to queue
    its own toast, which the user saw as 'Importing x…' appearing,
    vanishing, and appearing again. Repeats are dropped: they are
    the same work, and they must not inflate the collapsed count
    either."""
    if basename in window_names:
        return window_names, False
    return window_names + [basename], True


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
