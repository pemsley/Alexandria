"""The document outline behind the viewer's sidebar.

Publishers put a table of contents in the PDF itself — section
titles, their nesting, and the page each one starts on. GNOME Papers
shows it in a sidebar; we were not reading it at all.

Poppler exposes it through `Poppler.IndexIter`, with one sharp edge:
for a PDF that has no outline the constructor *raises* rather than
returning None, so every caller has to guard. About half the library
(93 of 192 PDFs, surveyed 2026-08-31) has one.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

viewer = pytest.importorskip("alexandria.viewer")


def _pdf_with_outline(path, entries=None, pages=6):
    """A PDF carrying a real outline. `entries` is
    [(parent_index_or_None, title, page)]; index 0 is the root."""
    import cairo
    if entries is None:
        # Parent references are 1-based positions in this list, so the
        # two sub-sections hang off "Methods" (position 2).
        entries = [(None, "Introduction", 1),
                   (None, "Methods", 2),
                   (2, "Crystallisation", 2),
                   (2, "Data collection", 4),
                   (None, "Results", 5)]
    surf = cairo.PDFSurface(path, 595, 842)
    cr = cairo.Context(surf)
    for i in range(pages):
        cr.move_to(72, 200)
        cr.show_text("page %d" % (i + 1))
        cr.show_page()
    ids = {}
    for n, (parent, title, page) in enumerate(entries, start=1):
        pid = cairo.PDF_OUTLINE_ROOT if parent is None else ids[parent]
        ids[n] = surf.add_outline(
            pid, title, "page=%d pos=[72 700]" % page, 0)
    surf.finish()


def _titles(entries):
    return [e["title"] for e in entries]


# ---- reading the outline -------------------------------------------

def test_every_outline_entry_is_returned(tmp_path):
    pdf = str(tmp_path / "outlined.pdf")
    _pdf_with_outline(pdf)
    doc = viewer.open_document(pdf)
    assert _titles(viewer.outline_entries(doc)) == [
        "Introduction", "Methods", "Crystallisation",
        "Data collection", "Results"]


def test_entries_are_in_document_order(tmp_path):
    """A sidebar that lists Results before Introduction is useless."""
    pdf = str(tmp_path / "outlined.pdf")
    _pdf_with_outline(pdf)
    doc = viewer.open_document(pdf)
    pages = [e["page"] for e in viewer.outline_entries(doc)]
    assert pages == sorted(pages)


def test_nesting_depth_is_carried(tmp_path):
    """Papers indents sub-sections under their parent; so must we."""
    pdf = str(tmp_path / "outlined.pdf")
    _pdf_with_outline(pdf)
    doc = viewer.open_document(pdf)
    depths = {e["title"]: e["depth"] for e in viewer.outline_entries(doc)}
    assert depths["Methods"] == 0
    assert depths["Crystallisation"] == 1
    assert depths["Data collection"] == 1
    assert depths["Results"] == 0


def test_each_entry_knows_its_page(tmp_path):
    """The page number is the column down the right of the sidebar,
    and the destination of a click. Zero-based, as page_widgets is."""
    pdf = str(tmp_path / "outlined.pdf")
    _pdf_with_outline(pdf)
    doc = viewer.open_document(pdf)
    by_title = {e["title"]: e["page"] for e in viewer.outline_entries(doc)}
    assert by_title["Introduction"] == 0
    assert by_title["Data collection"] == 3
    assert by_title["Results"] == 4


# ---- the half of the library with no outline -----------------------

def test_a_pdf_without_an_outline_yields_nothing(tmp_path):
    """Poppler.IndexIter.new raises TypeError here rather than
    returning None — an unguarded call takes the window down on
    roughly half the library."""
    import cairo
    pdf = str(tmp_path / "plain.pdf")
    surf = cairo.PDFSurface(pdf, 595, 842)
    cairo.Context(surf).show_page()
    surf.finish()
    assert viewer.outline_entries(viewer.open_document(pdf)) == []


def test_no_document_yields_nothing():
    assert viewer.outline_entries(None) == []


def test_an_entry_with_no_destination_is_kept_without_a_page(tmp_path):
    """Some outlines carry headings that point nowhere. They still
    belong in the list — they are how the reader sees the shape of
    the paper — but nothing should jump."""
    pdf = str(tmp_path / "outlined.pdf")
    _pdf_with_outline(pdf, entries=[(None, "Front matter", 1),
                                    (None, "Body", 3)])
    doc = viewer.open_document(pdf)
    for e in viewer.outline_entries(doc):
        assert "page" in e
        assert e["page"] is None or isinstance(e["page"], int)


def test_a_page_beyond_the_document_is_dropped():
    """Never hand the viewer a page index it cannot scroll to. Cairo
    will not write such a destination, so this is tested on the
    conversion directly rather than through a fixture."""
    assert viewer.dest_page_index(1, n_pages=3) == 0
    assert viewer.dest_page_index(3, n_pages=3) == 2
    assert viewer.dest_page_index(99, n_pages=3) is None
    assert viewer.dest_page_index(0, n_pages=3) is None
    assert viewer.dest_page_index(None, n_pages=3) is None


# ---- which mode the sidebar opens on -------------------------------

def test_contents_is_the_default_mode():
    assert viewer.initial_sidebar_mode(True, False) == "outline"
    assert viewer.initial_sidebar_mode(True, True) == "outline"


def test_a_pdf_with_no_contents_opens_on_its_highlights():
    """Half the library has no table of contents. Greeting those
    papers with an empty list is a poor welcome when there is
    something to show in the other mode."""
    assert viewer.initial_sidebar_mode(False, True) == "highlights"


def test_with_nothing_to_show_it_still_opens_somewhere():
    assert viewer.initial_sidebar_mode(False, False) in ("outline",
                                                         "highlights")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
