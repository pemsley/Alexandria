"""Status lines that arrive together must not overwrite each other.

Reported 2026-09-06 watching Get PDF: "if it's there it's there for
less than 100ms". The source lookups answer back to back —
`Unpaywall: …` and `Asking EuropePMC…` were emitted with no I/O
between them — so every verdict was overwritten before it could be
read, and only the download lines survived because real time passes
between those.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria.status_ticker import StatusTicker


class FakeClock:
    """Collects scheduled callbacks instead of running a main loop."""

    def __init__(self):
        self.shown = []
        self.timers = []

    def show(self, message):
        self.shown.append(message)

    def schedule(self, delay_ms, fn):
        self.timers.append((delay_ms, fn))

    def tick(self, times=1):
        """Fire the pending timers, as GLib would after the dwell."""
        for _ in range(times):
            if not self.timers:
                return
            _delay, fn = self.timers.pop(0)
            fn()


def test_back_to_back_milestones_are_shown_one_at_a_time():
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)

    t.post("Unpaywall: no open-access copy known")
    t.post("Asking EuropePMC…")
    t.post("EuropePMC: 1 PDF")

    # Only the first is on screen; the others wait their turn.
    assert c.shown == ["Unpaywall: no open-access copy known"]
    c.tick()
    assert c.shown[-1] == "Asking EuropePMC…"
    c.tick()
    assert c.shown[-1] == "EuropePMC: 1 PDF"


def test_each_milestone_is_held_for_the_dwell():
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)

    t.post("one")
    t.post("two")

    assert [d for d, _fn in c.timers] == [200]
    c.tick()
    assert [d for d, _fn in c.timers] == [200]


def test_the_queue_drains_and_releases_the_line():
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)
    t.post("one")
    c.tick()                       # nothing queued behind it

    t.post("bytes", transient=True)

    assert c.shown[-1] == "bytes"


def test_transient_updates_never_displace_a_milestone():
    """A byte count must not steal the line from a verdict that has
    not been read yet — and must not queue behind it either, because
    by then it is stale."""
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)

    t.post("EuropePMC: 1 PDF")
    t.post("Downloading — 1.2 MB", transient=True)
    t.post("Downloading — 2.4 MB", transient=True)

    assert c.shown == ["EuropePMC: 1 PDF"]
    c.tick()
    assert c.shown == ["EuropePMC: 1 PDF"]   # the stale ones are gone


def test_transient_updates_flow_once_the_milestones_are_done():
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)
    t.post("Connecting to europepmc.org…")
    c.tick()

    for mb in (1, 2, 3):
        t.post("Downloading — {} MB".format(mb), transient=True)

    assert c.shown[-3:] == ["Downloading — 1 MB", "Downloading — 2 MB",
                            "Downloading — 3 MB"]


def test_a_milestone_arriving_mid_download_takes_the_line_back():
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)
    t.post("Connecting…")
    c.tick()
    t.post("Downloading — 1 MB", transient=True)

    t.post("Got the PDF from europepmc.org")

    assert c.shown[-1] == "Got the PDF from europepmc.org"


def test_callback_passes_transience_through():
    c = FakeClock()
    t = StatusTicker(c.show, c.schedule, dwell_ms=200)
    cb = t.callback()

    cb("milestone")
    cb("bytes", True)          # dropped: the milestone holds the line

    assert c.shown == ["milestone"]
