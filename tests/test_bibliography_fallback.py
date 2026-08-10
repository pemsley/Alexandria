"""Tests for references_pdf._find_bibliography_fallback on two-column
layouts where poppler emits lines y-sorted *across* columns.

Models the 2025-era Nature Article layout (e.g.
10.1038/s41586-025-09761-x): no "References" heading at all — the
numbered list starts at the bottom of column 1 (entries 1–3) and
continues from the *top* of column 2 (entries 4+). In raw document
order the column-2 markers therefore come first (smaller y), so the
strict 1,2,3,… contiguity scan never sees a run longer than the
column-1 stub and gives up. The fallback must scan in column-aware
reading order instead.

Also covers figure axis-tick noise: a line like "0.7" matches
_ENTRY_RE with n=0 (digit, dot, rest); such candidates must be
ignored — real bibliography entries start at 1, and an interposed
n=0 candidate would otherwise break the contiguous run.

Runnable as `python3 -m tests.test_bibliography_fallback` or via
pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import references_pdf as R


def _rec(page, text, x, y):
    return (page, text, x, y, x + 1.0, y + 1.0)


def _two_column_page(page=6):
    """Column 1 (x≈40/54) holds entries 1–3 at the bottom of the page;
    column 2 (x≈306/320) holds entries 4–8 from the top. Records are
    listed y-sorted across columns — the order poppler hands them
    over in for this layout."""
    col2 = [
        _rec(page, "4.", 306.1, 70.0),
        _rec(page, "Xu, Z. et al. Meta-gradient reinforcement …", 320.3, 70.0),
        _rec(page, "5.", 306.1, 90.0),
        _rec(page, "Houthooft, R. et al. Evolved policy gradients.", 320.3, 90.0),
        _rec(page, "6.", 306.1, 110.0),
        _rec(page, "Lu, C. et al. Discovered policy optimisation.", 320.3, 110.0),
        _rec(page, "7.", 306.1, 130.0),
        _rec(page, "Silver, D. et al. Mastering the game of Go …", 320.3, 130.0),
        _rec(page, "8.", 306.1, 150.0),
        _rec(page, "Schrittwieser, J. et al. Mastering Atari, Go …", 320.3, 150.0),
    ]
    col1 = [
        _rec(page, "1.", 39.7, 679.9),
        _rec(page, "Kirsch, L., van Steenkiste, S. & Schmidhuber, J.", 53.8, 679.9),
        _rec(page, "2.", 39.7, 703.9),
        _rec(page, "Kirsch, L. et al. Introducing symmetries to …", 53.8, 703.9),
        _rec(page, "3.", 39.7, 728.0),
        _rec(page, "Oh, J. et al. Discovering reinforcement learning", 53.8, 728.0),
    ]
    # y-sorted interleave across columns: all of col2 (y 70–150)
    # precedes all of col1 (y 679.9–728) in the raw stream.
    return col2 + col1


def test_finds_run_split_across_columns():
    lines = _two_column_page()
    found = R._find_bibliography_fallback(lines)
    assert found is not None
    page, x, y = found
    assert page == 6
    assert abs(x - 39.7) < 0.01   # entry 1's marker, bottom of column 1
    assert abs(y - 679.9) < 0.01


def test_axis_tick_zero_candidates_are_ignored():
    # An axis tick label rendered between the markers of column 1
    # matches _ENTRY_RE as n=0; it must not break the 1,2,3,… run.
    lines = _two_column_page()
    lines.append(_rec(6, "0.7", 45.0, 690.0))
    found = R._find_bibliography_fallback(lines)
    assert found is not None
    _page, x, y = found
    assert abs(x - 39.7) < 0.01
    assert abs(y - 679.9) < 0.01


def test_short_run_still_rejected():
    # Only entries 1–3 exist (no column 2): under min_run=5 the scan
    # must keep returning None rather than latching onto a stub.
    lines = [rec for rec in _two_column_page() if rec[2] < 300]
    assert R._find_bibliography_fallback(lines) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
