"""Citation links anchored on the JATS, not guessed from the page.

Some journals mark citations as bare superscript numerals with no
brackets and no link annotations — Protein Science's Coot paper is
the case in hand, where all four existing paths find nothing and
the reader gets no clickable citations at all.

The JATS says exactly where each citation is, in words, and which
reference it points to:

    …such as ProSMART and LIBG.<xref rid="…bib-0003"
                                     ref-type="bibr">3</xref>

So the words before the marker become an anchor to find in the PDF's
text layout, and the rectangle comes from the glyphs that follow.
What to look for and where it points come from the publisher; only
the coordinates come from the page.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import jats

XML = """<article><body>
<p>Restraints for cryo-EM reconstructions come from other programs
such as ProSMART and LIBG.<xref rid="b3" ref-type="bibr">3</xref>
Newly incorporated restraints augment previous RNA
Tools<xref rid="b4" ref-type="bibr">4</xref> and
RCrane.<xref rid="b5" ref-type="bibr">5</xref></p>
<p>Ranges are written like this<xref rid="b3"
ref-type="bibr">3</xref>&#8211;<xref rid="b5"
ref-type="bibr">5</xref>.</p>
</body><back><ref-list>
<ref id="b3"><label>3.</label><element-citation><person-group>
<name><surname>Nicholls</surname></name></person-group>
<year>2017</year></element-citation></ref>
<ref id="b4"><label>4.</label><element-citation><person-group>
<name><surname>Lu</surname></name></person-group>
<year>2003</year></element-citation></ref>
<ref id="b5"><label>5.</label><element-citation><person-group>
<name><surname>Keating</surname></name></person-group>
<year>2012</year></element-citation></ref>
</ref-list></back></article>"""


def _write(tmp_path, xml=XML, name="paper.pdf"):
    pdf = str(tmp_path / name)
    with open(jats.jats_path(pdf), "w", encoding="utf-8") as fh:
        fh.write(xml)
    return jats.jats_path(pdf)


# ---- extracting the markers ----------------------------------------

def test_every_bibr_xref_is_found(tmp_path):
    xrefs = jats.parse_xrefs(_write(tmp_path))
    assert len(xrefs) == 5


def test_each_marker_knows_its_reference_number(tmp_path):
    xrefs = jats.parse_xrefs(_write(tmp_path))
    assert [x["n"] for x in xrefs] == [3, 4, 5, 3, 5]


def test_the_marker_text_is_carried(tmp_path):
    xrefs = jats.parse_xrefs(_write(tmp_path))
    assert xrefs[0]["marker"] == "3"


def test_context_is_the_words_before_the_marker(tmp_path):
    xrefs = jats.parse_xrefs(_write(tmp_path))
    ctx = xrefs[0]["context"]
    assert ctx.endswith("ProSMART and LIBG.")
    assert "\n" not in ctx, "context should be whitespace-normalised"
    assert len(ctx) >= 20, "needs to be long enough to be unique"


def test_context_excludes_other_markers(tmp_path):
    """The second citation's context must not contain the digit of
    the first — the PDF renders those as superscripts we are trying
    to locate, not as part of the prose."""
    xrefs = jats.parse_xrefs(_write(tmp_path))
    ctx = xrefs[1]["context"]
    assert ctx.endswith("previous RNA Tools")
    assert "LIBG.3" not in ctx


def test_markup_inside_the_context_is_dropped(tmp_path):
    xml = ('<article><body><p>as shown in <italic>Coot</italic> '
           'here<xref rid="b3" ref-type="bibr">3</xref></p></body>'
           '<back><ref-list><ref id="b3"><label>3.</label>'
           '<element-citation><year>2017</year></element-citation>'
           '</ref></ref-list></back></article>')
    xrefs = jats.parse_xrefs(_write(tmp_path, xml))
    assert xrefs[0]["context"].endswith("as shown in Coot here")
    assert "<" not in xrefs[0]["context"]


# ---- robustness ------------------------------------------------------

def test_unlabelled_refs_are_numbered_by_position(tmp_path):
    xml = ('<article><body><p>text<xref rid="r2" ref-type="bibr">'
           '2</xref></p></body><back><ref-list>'
           '<ref id="r1"><element-citation><year>2001</year>'
           '</element-citation></ref>'
           '<ref id="r2"><element-citation><year>2002</year>'
           '</element-citation></ref>'
           '</ref-list></back></article>')
    xrefs = jats.parse_xrefs(_write(tmp_path, xml))
    assert xrefs[0]["n"] == 2


def test_xrefs_to_figures_and_tables_are_ignored(tmp_path):
    xml = ('<article><body><p>see <xref rid="f1" ref-type="fig">'
           'Figure 1</xref> and this<xref rid="b3" ref-type="bibr">'
           '3</xref></p></body><back><ref-list><ref id="b3">'
           '<label>3.</label><element-citation><year>2017</year>'
           '</element-citation></ref></ref-list></back></article>')
    xrefs = jats.parse_xrefs(_write(tmp_path, xml))
    assert [x["n"] for x in xrefs] == [3]


def test_a_marker_pointing_at_no_known_reference_is_dropped(tmp_path):
    xml = ('<article><body><p>text<xref rid="ghost" ref-type="bibr">'
           '9</xref></p></body><back><ref-list><ref id="b3">'
           '<label>3.</label><element-citation><year>2017</year>'
           '</element-citation></ref></ref-list></back></article>')
    assert jats.parse_xrefs(_write(tmp_path, xml)) == []


def test_missing_or_broken_file(tmp_path):
    assert jats.parse_xrefs(str(tmp_path / "nope.xml")) == []
    bad = str(tmp_path / "bad.jats.xml")
    with open(bad, "w") as fh:
        fh.write("<article><body>truncated")
    assert jats.parse_xrefs(bad) == []


def test_an_article_with_no_citations(tmp_path):
    xml = "<article><body><p>no citations here</p></body></article>"
    assert jats.parse_xrefs(_write(tmp_path, xml)) == []


# ---- matching the anchor against page text -------------------------

from alexandria import references_pdf as R  # noqa: E402


def test_normalisation_makes_pdf_and_jats_text_comparable():
    """The JATS carries typographic characters the PDF renders
    differently — a non-breaking hyphen in 'cryo-EM' cost two of the
    twelve anchors in the first feasibility run."""
    jats_side = R._normalise_for_anchor("cryo‐EM maps, EMD‐3908")
    pdf_side = R._normalise_for_anchor("cryo-EM  maps,\nEMD-3908")
    assert jats_side == pdf_side


def test_normalisation_folds_ligatures_and_quotes():
    assert R._normalise_for_anchor("the ﬁrst “word”") == \
        R._normalise_for_anchor('the first "word"')


def test_hyphenation_at_a_line_break_is_folded_away():
    """The page prints "reconstruc- tions"; the XML has the word
    whole. Both must fold to the same thing, or no anchor spanning a
    hyphenated word can ever match."""
    assert R._normalise_for_anchor("reconstruc-\ntions") == \
        R._normalise_for_anchor("reconstructions")


def test_every_folded_character_maps_back_to_its_source():
    """The rectangle for a citation comes from indexing poppler's
    per-character rects with these offsets, so a fold that shifted
    them would draw the clickable box over the wrong glyphs."""
    src = "cryo‐EM  “maps”"
    text, idx = R._normalise_indexed(src)
    assert len(text) == len(idx)
    assert [src[i].lower() for i in idx] == \
        ['c', 'r', 'y', 'o', 'e', 'm', '“', 'm', 'a', 'p', 's', '”']
    assert idx == sorted(idx), "offsets must stay in order"


# ---- placing the marker on the page --------------------------------

def _two_page_pdf(path, body, bib):
    """A PDF with body text on page 1 and a numbered reference on
    page 2, laid out one line per string."""
    import cairo
    surf = cairo.PDFSurface(path, 595, 842)
    cr = cairo.Context(surf)
    cr.select_font_face("sans-serif")
    cr.set_font_size(11)
    for i, line in enumerate(body):
        cr.move_to(72, 100 + 16 * i)
        cr.show_text(line)
    cr.show_page()
    for i, line in enumerate(bib):
        cr.move_to(72, 100 + 16 * i)
        cr.show_text(line)
    cr.show_page()
    surf.finish()


BIB = [{"n": 3, "page": 1, "y_top_poppler": 92.0},
       {"n": 4, "page": 1, "y_top_poppler": 108.0},
       {"n": 5, "page": 1, "y_top_poppler": 124.0}]


def test_a_bare_superscript_citation_is_located(tmp_path):
    """The case the four older paths all miss: no brackets, no link
    annotation, nothing on the page that says "citation"."""
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["Restraints for cryo-EM reconstructions come "
                        "from other programs",
                        "such as ProSMART and LIBG.3 Newly "
                        "incorporated restraints augment",
                        "previous RNA Tools4 and RCrane.5"],
                  ["3. Nicholls 2017", "4. Lu 2003", "5. Keating 2012"])
    _write(tmp_path, XML)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    links = R.find_jats_citations(pdf, BIB, xrefs)
    assert links, "expected citation links on the body page"
    assert list(links) == [0], "page 2 holds the bibliography"
    assert [n for _r, _p, _t, n in links[0]] == [3, 4, 5]


def test_the_link_points_at_the_right_reference(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["such as ProSMART and LIBG.3 Newly "
                        "incorporated restraints augment"],
                  ["3. Nicholls 2017"])
    _write(tmp_path, XML)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    links = R.find_jats_citations(pdf, BIB, xrefs)
    rect, target_page, target_top, n = links[0][0]
    assert (n, target_page) == (3, 1)
    assert target_top == 842.0 - 92.0


def test_the_rect_covers_the_marker_and_not_the_word(tmp_path):
    """The rectangle is the digit's own glyph — a box over the whole
    sentence would make the page one big link."""
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["such as ProSMART and LIBG.3 Newly "
                        "incorporated restraints augment"],
                  ["3. Nicholls 2017"])
    _write(tmp_path, XML)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    (x1, y1, x2, y2), _p, _t, _n = R.find_jats_citations(
        pdf, BIB, xrefs)[0][0]
    assert 0 < x2 - x1 < 12, "one digit wide"
    assert 0 < y2 - y1 < 20


def test_a_word_broken_across_lines_still_anchors(tmp_path):
    """The typesetter hyphenates; the publisher's XML does not."""
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["Restraints for cryo-EM three-dimensional "
                        "reconstruc-",
                        "tions from other programs such as ProSMART "
                        "and LIBG.3 Newly"],
                  ["3. Nicholls 2017"])
    xml = XML.replace("cryo-EM reconstructions",
                      "cryo-EM three-dimensional reconstructions")
    _write(tmp_path, xml)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    links = R.find_jats_citations(pdf, BIB, xrefs)
    assert [n for _r, _p, _t, n in links.get(0, [])][:1] == [3]


def test_an_earlier_marker_inside_the_anchor_is_tolerated(tmp_path):
    """The XML drops the markers the page prints, so the anchor for
    the second citation of a sentence contains a digit the page has
    and the XML does not."""
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["Newly incorporated restraints augment "
                        "previous RNA Tools4 and RCrane.5"],
                  ["4. Lu 2003", "5. Keating 2012"])
    _write(tmp_path, XML)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    links = R.find_jats_citations(pdf, BIB, xrefs)
    assert 5 in [n for _r, _p, _t, n in links.get(0, [])]


def test_adjacent_markers_get_a_rect_each(tmp_path):
    """"…functionality2,6" is two citations, not one."""
    xml = ('<article><body><p>Model validation has been a mainstay of '
           'Coot functionality<xref rid="b3" ref-type="bibr">3</xref>,'
           '<xref rid="b4" ref-type="bibr">4</xref> that is worth '
           'keeping.</p></body><back><ref-list>'
           '<ref id="b3"><label>3.</label><element-citation>'
           '<year>2017</year></element-citation></ref>'
           '<ref id="b4"><label>4.</label><element-citation>'
           '<year>2003</year></element-citation></ref>'
           '</ref-list></back></article>')
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["Model validation has been a mainstay of Coot "
                        "functionality3,4 that is worth keeping."],
                  ["3. Nicholls 2017", "4. Lu 2003"])
    _write(tmp_path, xml)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    links = R.find_jats_citations(pdf, BIB, xrefs)
    got = links.get(0, [])
    assert [n for _r, _p, _t, n in got] == [3, 4]
    assert got[0][0][0] < got[1][0][0], "3 sits left of 4"


def test_a_citation_whose_prose_is_not_on_the_page_is_skipped(tmp_path):
    """No guess, no link: the anchor is the whole evidence."""
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["Some entirely unrelated body text with a "
                        "stray 3 in it."],
                  ["3. Nicholls 2017"])
    _write(tmp_path, XML)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    assert R.find_jats_citations(pdf, BIB, xrefs) == {}


def test_no_bibliography_positions_means_no_links(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["such as ProSMART and LIBG.3"],
                  ["3. Nicholls 2017"])
    _write(tmp_path, XML)
    xrefs = jats.parse_xrefs(jats.jats_path(pdf))
    assert R.find_jats_citations(pdf, [], xrefs) == {}
    assert R.find_jats_citations(pdf, BIB, []) == {}


def test_an_unreadable_pdf_yields_no_links(tmp_path):
    bad = str(tmp_path / "bad.pdf")
    with open(bad, "wb") as fh:
        fh.write(b"not a pdf")
    _write(tmp_path, XML, name="bad.pdf")
    xrefs = jats.parse_xrefs(jats.jats_path(bad))
    assert R.find_jats_citations(bad, BIB, xrefs) == {}


# ---- how the viewer reaches Path E ---------------------------------

from alexandria import viewer  # noqa: E402


def test_path_e_is_not_consulted_without_a_jats_file(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["such as ProSMART and LIBG.3"],
                  ["3. Nicholls 2017"])
    assert viewer._jats_citation_links(pdf, BIB) == {}


def test_path_e_runs_when_there_is_a_jats_file(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["such as ProSMART and LIBG.3 Newly "
                        "incorporated restraints augment"],
                  ["3. Nicholls 2017"])
    _write(tmp_path, XML)
    links = viewer._jats_citation_links(pdf, BIB)
    assert [n for _r, _p, _t, n in links[0]] == [3]


def test_a_bare_superscript_paper_gets_links_end_to_end(tmp_path):
    """What the user reported: a paper where every other path finds
    nothing, so the reader gets no clickable citations at all."""
    pdf = str(tmp_path / "paper.pdf")
    _two_page_pdf(pdf, ["Restraints for cryo-EM reconstructions come "
                        "from other programs",
                        "such as ProSMART and LIBG.3 Newly "
                        "incorporated restraints augment",
                        "previous RNA Tools4 and RCrane.5"],
                  ["References", "3. Nicholls A. 2017 Acta Cryst.",
                   "4. Lu X. 2003 Nucleic Acids Res.",
                   "5. Keating K. 2012 RNA."])
    assert viewer.build_citation_links(pdf) == {}, \
        "no JATS: this is the paper the user could not click through"
    _write(tmp_path, XML)
    links = viewer.build_citation_links(pdf)
    assert sorted(n for plinks in links.values()
                  for _r, _p, _t, n in plinks) == [3, 4, 5]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
