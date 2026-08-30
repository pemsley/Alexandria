"""How often the card list may rebuild.

Every import schedules a rebuild of every card 300 ms later. During a
bulk import that means one full rebuild per imported file, each one
competing with the import threads for the GIL and for SQLite — with
33 papers in, the main loop was already freezing for 4.7 s at a
time, and the cost grows with the library.

A rebuild is worth doing promptly for a single change and worth
rationing during a burst.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import reload_policy as rp


def test_first_reload_uses_the_plain_debounce():
    assert rp.reload_delay_ms(now=100.0, last_reload_at=None) == 300


def test_a_quiet_library_still_reloads_promptly():
    """Nothing has rebuilt for a while: no reason to wait."""
    assert rp.reload_delay_ms(now=100.0, last_reload_at=90.0) == 300


def test_a_burst_is_rationed():
    """A second change 0.5 s after the last rebuild waits out the
    remainder of the minimum interval instead of rebuilding again."""
    got = rp.reload_delay_ms(now=100.5, last_reload_at=100.0,
                             min_interval_ms=5000)
    assert got == 4500


def test_the_delay_never_drops_below_the_debounce():
    got = rp.reload_delay_ms(now=104.9, last_reload_at=100.0,
                             min_interval_ms=5000)
    assert got == 300


def test_exactly_at_the_interval_is_prompt():
    got = rp.reload_delay_ms(now=105.0, last_reload_at=100.0,
                             min_interval_ms=5000)
    assert got == 300


def test_a_storm_never_starves_the_final_rebuild():
    """However long the burst runs, the next rebuild is always
    scheduled — rationing delays it, it never cancels it."""
    now, last = 100.0, 100.0
    for _ in range(50):
        d = rp.reload_delay_ms(now=now, last_reload_at=last)
        assert d > 0
        now += 0.1
    assert rp.reload_delay_ms(now=last + 60, last_reload_at=last) == 300


def test_clock_going_backwards_is_tolerated():
    """Monotonic clocks shouldn't, but a bad value must not produce
    a negative timeout."""
    got = rp.reload_delay_ms(now=99.0, last_reload_at=100.0)
    assert got >= 300


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


def test_no_rebuild_while_a_bulk_import_is_running():
    """During Import Folder the progress bar is the feedback; a
    rebuild competes with the import threads for the GIL and gets a
    sliver of it (measured: 32 s to rebuild 136 cards, work that
    takes 0.5 s uncontended). The import's own completion path
    rebuilds once at the end."""
    assert rp.reload_delay_ms(now=100.0, last_reload_at=None,
                              import_busy=True) is None
    assert rp.reload_delay_ms(now=100.0, last_reload_at=90.0,
                              import_busy=True) is None


def test_rebuilds_resume_once_the_import_finishes():
    assert rp.reload_delay_ms(now=100.0, last_reload_at=90.0,
                              import_busy=False) == 300
