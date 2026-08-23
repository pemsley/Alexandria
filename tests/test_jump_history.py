"""Tests for the viewer's citation-jump history — the "back to where
I was" round trip.

`_current_position` / `_jump_to` are a coordinate pair: the first
converts the scrollbar's pixel offset into `(page, y_pdf_up)` and the
second converts it back. They have to agree, including at a different
zoom level than the one the position was recorded at, because the
reader can zoom while reading the reference.

The logic is exercised against a stand-in with the same handful of
attributes the real widget exposes (`page_y`, `zoom`, `n_pages`, a
`doc` with page sizes and a scrolled window's vadjustment). That
keeps the test off GTK's display connection while still running the
real methods.

Runnable as `python3 -m tests.test_jump_history` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


PAGE_H_PT = 792.0     # US Letter, the height every fake page reports
PAGE_GAP = 10         # pixels between stacked pages


class _FakePage:
    def get_size(self):
        return (612.0, PAGE_H_PT)


class _FakeDoc:
    def get_page(self, _i):
        return _FakePage()


class _FakeAdj:
    def __init__(self):
        self.value = 0.0

    def get_value(self):
        return self.value

    def set_value(self, v):
        self.value = v


class _FakeScrolled:
    def __init__(self):
        self.adj = _FakeAdj()

    def get_vadjustment(self):
        return self.adj


class _FakeViewer:
    """Just enough of the viewer for the two coordinate methods."""

    n_pages = 4

    def __init__(self, zoom=1.0):
        self.zoom = zoom
        self.doc = _FakeDoc()
        self.scrolled = _FakeScrolled()
        self.current_page = 0
        self._jump_stack = []
        self._back_visible = False
        self._recompute_page_y()

    def _recompute_page_y(self):
        self.page_y = {}
        y = 0
        for i in range(self.n_pages):
            self.page_y[i] = y
            y += int(PAGE_H_PT * self.zoom) + PAGE_GAP

    def set_zoom(self, z):
        self.zoom = z
        self._recompute_page_y()

    # The methods under test, bound off the real class.
    from alexandria.viewer import PdfViewerWindow as _Real
    _current_position = _Real._current_position
    _push_jump_origin = _Real._push_jump_origin
    _jump_back = _Real._jump_back

    def _update_back_btn(self):
        self._back_visible = bool(self._jump_stack)

    def _jump_to(self, page_idx, top_pdf_up=None):
        """Mirror of the real `_jump_to` scroll maths, minus the
        idle_add and the widget poking."""
        if page_idx < 0 or page_idx >= self.n_pages:
            return
        self.current_page = page_idx
        ph_pt = PAGE_H_PT
        offset_in_page = 0
        if top_pdf_up is not None and 0 <= top_pdf_up <= ph_pt:
            offset_in_page = (ph_pt - top_pdf_up) * self.zoom
        self.scrolled.adj.set_value(self.page_y[page_idx] + offset_in_page)


def _scrolled_to(v, y):
    v.scrolled.adj.set_value(y)


# ---- Position capture ----

def test_position_at_top_of_first_page():
    v = _FakeViewer()
    _scrolled_to(v, 0)
    assert v._current_position() == (0, PAGE_H_PT)


def test_position_identifies_the_right_page():
    v = _FakeViewer()
    _scrolled_to(v, v.page_y[2] + 100)
    page, y = v._current_position()
    assert page == 2, (page, y)
    assert abs(y - (PAGE_H_PT - 100)) < 0.01, y


def test_position_in_the_gap_between_pages_clamps():
    # The viewport top can land in the inter-page gap, which would
    # otherwise yield a y outside the page box that _jump_to discards.
    v = _FakeViewer()
    _scrolled_to(v, v.page_y[1] - 4)
    page, y = v._current_position()
    assert 0.0 <= y <= PAGE_H_PT, (page, y)


# ---- Round trip ----

def test_round_trip_returns_to_the_same_pixel():
    v = _FakeViewer()
    _scrolled_to(v, v.page_y[1] + 250)
    origin = v.scrolled.adj.get_value()
    v._push_jump_origin()
    v._jump_to(3, 400.0)                 # follow the citation
    assert v.scrolled.adj.get_value() != origin
    v._jump_back()
    assert abs(v.scrolled.adj.get_value() - origin) < 0.01


def test_round_trip_survives_a_zoom_change():
    # The whole reason positions are stored in PDF points: zooming
    # while reading the reference must not move the spot we return to.
    v = _FakeViewer()
    _scrolled_to(v, v.page_y[1] + 250)
    page_before, y_before = v._current_position()
    v._push_jump_origin()
    v._jump_to(3, 400.0)
    v.set_zoom(1.5)
    v._jump_back()
    assert v.current_page == page_before
    page_after, y_after = v._current_position()
    assert page_after == page_before
    assert abs(y_after - y_before) < 0.01, (y_before, y_after)


# ---- Stack behaviour ----

def test_stack_unwinds_one_step_at_a_time():
    v = _FakeViewer()
    _scrolled_to(v, v.page_y[0] + 50)
    first = v.scrolled.adj.get_value()
    v._push_jump_origin()
    v._jump_to(3, 400.0)
    second = v.scrolled.adj.get_value()
    v._push_jump_origin()               # a citation inside the reference
    v._jump_to(2, 200.0)
    v._jump_back()
    assert abs(v.scrolled.adj.get_value() - second) < 0.01
    v._jump_back()
    assert abs(v.scrolled.adj.get_value() - first) < 0.01


def test_jump_back_on_empty_stack_is_a_no_op():
    v = _FakeViewer()
    _scrolled_to(v, 123)
    v._jump_back()
    assert v.scrolled.adj.get_value() == 123


def test_back_affordance_tracks_the_stack():
    v = _FakeViewer()
    assert v._back_visible is False
    v._push_jump_origin()
    assert v._back_visible is True
    v._jump_back()
    assert v._back_visible is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
