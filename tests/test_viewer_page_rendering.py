"""Opening a paper must not render the whole paper.

Measured 2026-08-31 against GNOME Papers on ten library PDFs. Papers
puts its first frame on screen in a flat 0.51 s whatever the document
— 0.3 MB to 42.8 MB, one page to fifty-six. Alexandria ranged from
0.91 s to 20.51 s, scaling with the document rather than with the
window.

The cause was that every page lives in its own `Gtk.DrawingArea`
inside one `Gtk.Box`, so GTK snapshots the lot and `_draw_one_page`
renders every page before the first frame appears. On `pdf-3.pdf`
all forty pages drew, page 24 alone taking 4.87 s.

So: draw only what is near the viewport, render off the main thread,
and keep the result. A page not yet rendered shows as blank and fills
in when it arrives — which is what Papers does, and what makes
scrolling smooth once a page has been seen.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

viewer = pytest.importorskip("alexandria.viewer")


# ---- which pages are worth drawing at all --------------------------

def test_a_page_in_the_viewport_is_drawn():
    # viewport showing document y 0..800; page 1 occupies 0..842
    assert viewer.page_is_near(0, 842, view_top=0, view_height=800,
                               margin=0)


def test_a_page_far_below_the_viewport_is_not_drawn():
    """Page 24 of a forty-page paper, while page 1 is on screen. This
    is the whole bug: rendering it cost 4.87 s that nobody asked
    for."""
    assert not viewer.page_is_near(24 * 850, 842, view_top=0,
                                   view_height=800, margin=0)


def test_a_page_just_off_screen_is_drawn_when_a_margin_is_given():
    """A margin renders the neighbours, so scrolling a little does
    not show blank paper."""
    assert not viewer.page_is_near(900, 842, view_top=0,
                                   view_height=800, margin=0)
    assert viewer.page_is_near(900, 842, view_top=0, view_height=800,
                               margin=400)


def test_a_page_partly_scrolled_off_the_top_is_still_drawn():
    assert viewer.page_is_near(0, 842, view_top=800, view_height=800,
                               margin=0)


def test_the_page_the_viewport_sits_inside_is_drawn():
    """A page taller than the window: neither edge is in view."""
    assert viewer.page_is_near(0, 4000, view_top=1500, view_height=800,
                               margin=0)


def test_the_default_margin_is_about_a_screenful():
    assert viewer._RENDER_MARGIN_PX > 0


# ---- the cache does not grow without limit -------------------------

def test_pages_nearest_the_reader_are_kept():
    keep = viewer.pages_to_keep({1, 2, 3, 20, 21, 39}, current_page=2,
                                limit=3)
    assert keep == {1, 2, 3}


def test_nothing_is_evicted_below_the_limit():
    cached = {4, 5, 6}
    assert viewer.pages_to_keep(cached, current_page=5, limit=10) == cached


def test_the_current_page_is_never_evicted():
    keep = viewer.pages_to_keep({0, 1, 2, 30}, current_page=30, limit=1)
    assert keep == {30}


def test_the_cache_limit_is_bounded():
    """Each A4 page at 100% is about 2 MB of ARGB; an unbounded cache
    on a long paper would be worse than the bug it fixes."""
    assert 2 <= viewer._PAGE_CACHE_MAX <= 40


# ---- rendering a page off the main thread --------------------------

def _sample_pdf(path, pages=3):
    import cairo
    surf = cairo.PDFSurface(path, 595, 842)
    cr = cairo.Context(surf)
    for i in range(pages):
        cr.move_to(72, 200)
        cr.set_font_size(24)
        cr.show_text("page %d" % (i + 1))
        cr.show_page()
    surf.finish()


def test_render_produces_a_surface_of_the_zoomed_size(tmp_path):
    pdf = str(tmp_path / "sample.pdf")
    _sample_pdf(pdf)
    doc = viewer.open_document(pdf)
    surf = viewer.render_page_surface(doc, 0, 2.0)
    assert surf.get_width() == 1190
    assert surf.get_height() == 1684


def test_render_paints_the_page_on_white(tmp_path):
    """Not transparent: the sheet of paper is part of the render, so
    a page that has arrived never shows the window behind it."""
    pdf = str(tmp_path / "sample.pdf")
    _sample_pdf(pdf)
    doc = viewer.open_document(pdf)
    surf = viewer.render_page_surface(doc, 0, 1.0)
    data = bytes(surf.get_data()[:4])
    assert data == b"\xff\xff\xff\xff", "top-left pixel should be opaque white"


def test_each_page_renders_its_own_content(tmp_path):
    pdf = str(tmp_path / "sample.pdf")
    _sample_pdf(pdf)
    doc = viewer.open_document(pdf)
    a = bytes(viewer.render_page_surface(doc, 0, 1.0).get_data())
    b = bytes(viewer.render_page_surface(doc, 1, 1.0).get_data())
    assert a != b


def test_a_render_thread_gets_its_own_document(tmp_path):
    """Poppler documents are not safe to share across threads, the
    same lesson as the database connections. The renderer opens its
    own handle rather than borrowing the window's."""
    pdf = str(tmp_path / "sample.pdf")
    _sample_pdf(pdf)
    assert viewer.open_document(pdf) is not viewer.open_document(pdf)


def test_open_document_on_rubbish_returns_none(tmp_path):
    bad = str(tmp_path / "bad.pdf")
    with open(bad, "wb") as fh:
        fh.write(b"not a pdf")
    assert viewer.open_document(bad) is None
    assert viewer.open_document(str(tmp_path / "missing.pdf")) is None


def test_render_of_a_page_that_does_not_exist_is_none(tmp_path):
    pdf = str(tmp_path / "sample.pdf")
    _sample_pdf(pdf)
    doc = viewer.open_document(pdf)
    assert viewer.render_page_surface(doc, 99, 1.0) is None
    assert viewer.render_page_surface(None, 0, 1.0) is None


# ---- which waiting page to render next -----------------------------

def test_the_page_being_read_renders_first():
    """A jump to page 24 must not sit behind the pages queued while
    page 1 was on screen."""
    assert viewer.next_page_to_render({0, 1, 2, 23, 24}, 24) == 24


def test_the_nearest_waiting_page_wins():
    assert viewer.next_page_to_render({0, 1, 2, 30}, 24) == 30


def test_ties_go_to_the_earlier_page():
    assert viewer.next_page_to_render({4, 6}, 5) == 4


def test_nothing_waiting():
    assert viewer.next_page_to_render(set(), 3) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
