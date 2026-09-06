"""Hold each status message long enough to be read.

A fetch reports its steps as it makes them, and some of those steps
take no measurable time: the line "Unpaywall: no open-access copy
known" was emitted and then immediately followed by "Asking
EuropePMC…", with no I/O between the two. Written straight to a
label, it appeared for under 100 ms — which is to say, never.

So messages queue, and each *milestone* holds the line for a minimum
dwell before the next one replaces it. Byte-count updates are marked
transient instead: they are worth showing when the line is free, and
worth dropping when it is not, because the next one is along in a
moment anyway and a stale byte count is worse than a missing one.

GTK-free: the caller injects `show` and `schedule`, so the ordering
can be tested with a fake clock rather than a main loop.
"""

# Long enough to read a short line, short enough that a five-step
# lookup does not feel padded. 200 ms, chosen by the person watching
# it happen.
DEFAULT_DWELL_MS = 200


class StatusTicker:
    """Serialise status lines so none is overwritten before it is seen.

    `show(message)` puts a line on screen. `schedule(delay_ms, fn)`
    calls `fn` later — `GLib.timeout_add` in the app, a fake in the
    tests.
    """

    def __init__(self, show, schedule, dwell_ms=DEFAULT_DWELL_MS):
        self._show = show
        self._schedule = schedule
        self._dwell_ms = dwell_ms
        self._pending = []
        self._holding = False

    def post(self, message, transient=False):
        """Queue `message`. A transient one is shown only if the line
        is free, and never queued — it is superseded by the next
        update rather than delaying anything behind it."""
        if transient:
            if not self._holding:
                self._show(message)
            return
        self._pending.append(message)
        if not self._holding:
            self._advance()

    def _advance(self):
        """Show the next queued milestone, or release the line."""
        if not self._pending:
            self._holding = False
            return False
        self._show(self._pending.pop(0))
        self._holding = True
        self._schedule(self._dwell_ms, self._advance)
        return False

    def callback(self):
        """A `(message, transient=False)` callable for pdf_fetch's
        `on_progress`."""
        def on_progress(message, transient=False):
            self.post(message, transient=transient)
        return on_progress
