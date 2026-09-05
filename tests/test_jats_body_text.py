"""Reading the stored JATS as text, for summarising.

Found 2026-09-04: `SUMMARY_SOURCES` offers "jats", `set_summary`
documents it and `summary_overview` counts by it — but none of the
MCP server's eleven tools could open the JATS. `get_pdf_texts` was
the only text tool, so a client could *claim* it had read the
structured full text while having no way to do so.

The difference is not small. On the Coot paper the PDF extraction is
46,722 characters against the JATS body's 42,259 — and the extra
4,463 are not content but nine repetitions of

    1469896x, 2020, 4, Downloaded from https://onlinelibrary...
    by NICE, National Institute for Health and Care Excellence...

plus the column-order and hyphenation damage the viewer's citation
work spent a day compensating for.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import jats

XML = """<article><front><article-meta>
<abstract><p>We describe a thing.</p></abstract>
</article-meta></front><body>
<sec><title>INTRODUCTION</title>
<p>The first paragraph, which runs on
across several lines in the source.</p>
<p>A second paragraph citing
<xref ref-type="bibr" rid="b1">1</xref> something.</p>
<sec><title>Fitting domains</title>
<p>A nested subsection.</p></sec>
</sec>
<sec><title>METHODS</title>
<p>How it was done.</p>
<fig><caption><p>Figure caption text.</p></caption></fig>
</sec>
</body><back><ref-list><ref id="b1"><label>1.</label>
<mixed-citation>Someone. A paper. 2020.</mixed-citation>
</ref></ref-list></back></article>"""


def _write(tmp_path, xml=XML):
    p = str(tmp_path / "paper.jats.xml")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(xml)
    return p


# ---- the text ---------------------------------------------------------

def test_the_body_prose_is_returned(tmp_path):
    t = jats.body_text(_write(tmp_path))
    assert "The first paragraph" in t
    assert "How it was done." in t


def test_section_titles_are_kept_as_headings(tmp_path):
    """Structure the publisher marked up is worth keeping — it tells
    a reader (or a summariser) which part of the paper it is in."""
    t = jats.body_text(_write(tmp_path))
    assert "INTRODUCTION" in t
    assert "METHODS" in t
    assert "Fitting domains" in t


def test_nesting_is_visible_in_the_heading_level(tmp_path):
    t = jats.body_text(_write(tmp_path))
    lines = [l for l in t.splitlines() if l.startswith("#")]
    assert any(l.startswith("# ") and "INTRODUCTION" in l for l in lines)
    assert any(l.startswith("## ") and "Fitting domains" in l
               for l in lines)


def test_paragraphs_are_separated(tmp_path):
    t = jats.body_text(_write(tmp_path))
    assert "\n\n" in t


def test_a_wrapped_source_line_becomes_one_paragraph(tmp_path):
    """The XML wraps at 80 columns; that is not a line break in the
    prose, and leaving it in is what makes extracted PDF text
    awkward to read."""
    t = jats.body_text(_write(tmp_path))
    assert "runs on across several lines" in t


def test_citation_markers_stay_in_the_prose(tmp_path):
    """`<xref>1</xref>` is a citation the reader sees; dropping it
    would silently change sentences."""
    t = jats.body_text(_write(tmp_path))
    assert "citing 1 something" in t


def test_the_reference_list_is_not_body_text(tmp_path):
    """40 references would swamp a summary, and parse_ref_list
    already serves them properly."""
    t = jats.body_text(_write(tmp_path))
    assert "Someone. A paper." not in t


# ---- robustness --------------------------------------------------------

def test_a_missing_or_broken_file_yields_nothing(tmp_path):
    assert jats.body_text(str(tmp_path / "nope.xml")) == ""
    bad = str(tmp_path / "bad.jats.xml")
    with open(bad, "w") as fh:
        fh.write("<article><body>truncated")
    assert jats.body_text(bad) == ""


def test_an_article_with_no_body(tmp_path):
    xml = "<article><front/></article>"
    assert jats.body_text(_write(tmp_path, xml)) == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
