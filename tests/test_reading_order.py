"""Regression test for references_pdf._sort_reading_order.

Reproduces the two-column reference-list bug where a hanging-indent
marker ("16.") sits a fraction of a point *below* the first line of its
own entry ("Brini, E., …"). A naive sort by y put the body line before
its marker, so the body attached to the previous reference and entry 16
held only the wrapped tail ("eaaz3041 (2020)."). The row-tolerant sort
must keep the marker immediately ahead of its body line.

Coordinates are the real ones observed in jumper2021highly.pdf p.6.

Runnable as `python3 -m tests.test_reading_order` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import references_pdf as R


# (page, text, x1, y1, x2, y2) — only page/text/x1/y1 are used.
def _rec(text, x, y):
    return (6, text, x, y, x + 1.0, y + 1.0)


# Column 1 (x≈40 marker / 54 body) interleaved in the source list with
# column 2 (x≈306/320). Note entry 16's marker y=469.9 is 0.1 *below*
# its first body line at 469.8 — the crux of the bug.
RECORDS = [
    _rec("15.", 39.7, 445.8),
    _rec("Moult, J., Fidelis, K. Critical assessment …", 53.8, 445.8),
    _rec("techniques for protein structure prediction …", 53.8, 453.8),
    _rec("52. Vaswani, A. et al. Attention is all you need.", 306.1, 455.9),
    _rec("https://www.predictioncenter.org/…", 53.8, 461.8),
    _rec("Systems 5998–6008 (2017).", 320.3, 464.0),
    _rec("Brini, E., Simmerling, C. & Dill, K. Protein …", 53.8, 469.8),
    _rec("16.", 39.7, 469.9),                       # 0.1pt below its body
    _rec("53. Wang, H. et al. Axial-deeplab …", 306.1, 472.0),
    _rec("eaaz3041 (2020).", 53.8, 477.9),
    _rec("17.", 39.7, 485.9),
    _rec("Sippl, M. J. Calculation of conformational …", 53.8, 485.9),
]

EDGES = [40.0, 306.0]


def _order():
    out = R._sort_reading_order(RECORDS, EDGES)
    return [r[1] for r in out]


def test_marker_16_precedes_its_body_line():
    texts = _order()
    i_marker = texts.index("16.")
    i_body = next(i for i, t in enumerate(texts) if t.startswith("Brini"))
    assert i_marker < i_body, (
        "marker 16. must sort before its body line 'Brini …'")
    assert texts[i_marker + 1].startswith("Brini"), (
        "body line must immediately follow its marker")


def test_equal_y_marker_still_first():
    # Entry 15's marker and body share the exact same y — must stay
    # marker-first via the x tiebreak.
    texts = _order()
    i_marker = texts.index("15.")
    i_body = next(i for i, t in enumerate(texts) if t.startswith("Moult"))
    assert i_marker < i_body


def test_columns_kept_in_reading_order():
    # All of column 1 comes before any of column 2 (per-column reading
    # order), so a column-2 line never splits a column-1 entry.
    texts = _order()
    col1_markers = [texts.index(m) for m in ("15.", "16.", "17.")]
    col2_first = next(i for i, t in enumerate(texts)
                      if t.startswith(("52.", "53.")))
    assert max(col1_markers) < col2_first


def test_markers_in_numeric_order():
    texts = _order()
    assert texts.index("15.") < texts.index("16.") < texts.index("17.")


# ---- Self-test runner (no pytest needed) ---------------------------


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        name = t.__name__
        try:
            t()
        except AssertionError as e:
            failures += 1
            print("FAIL  {}\n        {}".format(name, e))
        except Exception as e:
            failures += 1
            print("ERROR {}\n        {!r}".format(name, e))
        else:
            print("ok    {}".format(name))
    print()
    print("{} test(s), {} failure(s)".format(len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
