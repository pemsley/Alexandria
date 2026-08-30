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
