"""Opening the viewer must not parse the bibliography on the main
thread.

Measured 2026-08-30: PdfViewerWindow.__init__ called
references_pdf.bibliography_positions (plus parse_bibliography and
the text-based fallbacks) inline. Alone that costs ~0.5 s, but while
a concurrent import holds the GIL it stretched to a 43 s freeze —
the GTK main loop cannot run during any of it.

The work is now a module-level pure function so it can run on a
worker thread (and be tested without a window).
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

viewer = pytest.importorskip("alexandria.viewer")


def _blank_pdf(path):
    import cairo
    surf = cairo.PDFSurface(path, 595, 842)
    cr = cairo.Context(surf)
    cr.move_to(72, 720)
    cr.show_text("A paper with no links at all")
    cr.show_page()
    surf.finish()


def test_build_citation_links_is_module_level():
    assert callable(getattr(viewer, "build_citation_links", None)), \
        "the link build must be callable off the main thread"


def test_build_citation_links_on_a_plain_pdf(tmp_path):
    pdf = str(tmp_path / "plain.pdf")
    _blank_pdf(pdf)
    links = viewer.build_citation_links(pdf)
    assert isinstance(links, dict)
    # No annotations, no bibliography: nothing to hit-test.
    assert all(isinstance(v, list) for v in links.values())


def test_build_citation_links_survives_a_bad_path(tmp_path):
    """Never raise into the worker thread — a broken PDF must
    degrade to 'no links', exactly as the inline version did."""
    bad = str(tmp_path / "not-a.pdf")
    with open(bad, "wb") as fh:
        fh.write(b"not a pdf at all")
    assert viewer.build_citation_links(bad) == {}
    assert viewer.build_citation_links(
        str(tmp_path / "missing.pdf")) == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---- the hover cursor must survive links arriving late -------------

class _FakeWindow:
    """Just enough of PdfViewerWindow to exercise the wiring: a
    recording stand-in for the real controller attachment."""

    def __init__(self, n_pages):
        self.citation_links = {}
        self._citation_links_ready = False
        self._cursor_over_link = {}
        self.page_widgets = list(range(n_pages))
        self.attached = []

    def _attach_link_motion_controller(self, da, page_idx):
        self.attached.append(page_idx)

    apply_links = viewer.PdfViewerWindow._apply_citation_links


def test_motion_controllers_are_attached_when_links_arrive():
    """Regression: _attach_link_motion_controller skips pages with no
    links, and page widgets are built before the worker thread
    delivers any — so once link-building moved off the main thread,
    every page was skipped and the hover cursor never appeared."""
    win = _FakeWindow(3)
    assert win.attached == [], "nothing to instrument before links"

    win.apply_links({0: [((0, 0, 1, 1), 5, 0.0, 1)],
                     2: [((0, 0, 1, 1), 6, 0.0, 2)]})

    assert sorted(win.attached) == [0, 2], \
        "pages with links must get a motion controller once the " \
        "links land"


def test_pages_without_links_stay_uninstrumented():
    win = _FakeWindow(3)
    win.apply_links({1: [((0, 0, 1, 1), 4, 0.0, 3)]})
    assert win.attached == [1]


def test_an_empty_link_map_attaches_nothing():
    win = _FakeWindow(3)
    win.apply_links({})
    assert win.attached == []
