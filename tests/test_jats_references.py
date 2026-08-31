"""Reference lists from stored JATS, instead of parsed from the PDF.

Everything `references_pdf.parse_bibliography` reconstructs by
geometry and regex — where the section starts, which line begins
entry 7, whether "1998" is a year or a page number — the publisher
already marked up and then discarded when rendering the PDF. Where
we hold the JATS, read it instead: the entries are exact, and the
DOI arrives as data rather than as a guess.

Fragments here are real ones from the library's stored files.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import jats

ELEMENT_CITATION = """<?xml version="1.0"?>
<article><back><ref-list id="Bib1"><title>References</title>
<ref id="CR1"><label>1.</label><citation-alternatives>
<element-citation id="ec-CR1" publication-type="journal">
<person-group person-group-type="author">
<name><surname>Thompson</surname><given-names>MC</given-names></name>
<name><surname>Yeates</surname><given-names>TO</given-names></name>
</person-group>
<article-title>Advances in methods for atomic resolution</article-title>
<source>F1000Res.</source><year>2020</year><volume>9</volume>
<fpage>667</fpage>
<pub-id pub-id-type="doi">10.12688/f1000research.25097.1</pub-id>
</element-citation>
<mixed-citation id="mc-CR1">Thompson, M. C. &amp; Yeates, T. O.
Advances in methods for atomic resolution. <italic>F1000Res</italic>.
<bold>9</bold>, 667 (2020).</mixed-citation>
</citation-alternatives></ref>
<ref id="CR2"><label>2.</label>
<element-citation publication-type="journal">
<person-group person-group-type="author">
<name><surname>Read</surname><given-names>RJ</given-names></name>
</person-group>
<article-title>Improved Fourier coefficients</article-title>
<source>Acta Cryst. A</source><year>1986</year>
</element-citation></ref>
</ref-list></back></article>"""


def _write(tmp_path, xml, name="paper.pdf"):
    pdf = str(tmp_path / name)
    with open(jats.jats_path(pdf), "w", encoding="utf-8") as fh:
        fh.write(xml)
    return pdf


# ---- the fields the reference popover needs ------------------------

def test_entries_are_numbered_from_the_label(tmp_path):
    pdf = _write(tmp_path, ELEMENT_CITATION)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert [r["n"] for r in refs] == [1, 2]


def test_doi_comes_from_the_markup_not_a_search(tmp_path):
    pdf = _write(tmp_path, ELEMENT_CITATION)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert refs[0]["doi"] == "10.12688/f1000research.25097.1"
    assert refs[1]["doi"] is None


def test_first_author_surname_and_year_are_carried(tmp_path):
    """The author-year resolution path needs both."""
    pdf = _write(tmp_path, ELEMENT_CITATION)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert refs[0]["surname"] == "Thompson"
    assert refs[0]["year"] == 2020
    assert refs[1]["surname"] == "Read"
    assert refs[1]["year"] == 1986


def test_journal_is_carried_for_the_soft_match(tmp_path):
    pdf = _write(tmp_path, ELEMENT_CITATION)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert refs[1]["journal"] == "Acta Cryst. A"


def test_display_text_prefers_the_rendered_citation(tmp_path):
    """<mixed-citation> is the publisher's own rendering — closer to
    what the reader sees on the page than anything reassembled."""
    pdf = _write(tmp_path, ELEMENT_CITATION)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert "Thompson" in refs[0]["text"]
    assert "F1000Res" in refs[0]["text"]
    assert "<italic>" not in refs[0]["text"]
    assert "  " not in refs[0]["text"], "whitespace should be tidied"


def test_text_is_assembled_when_there_is_no_mixed_citation(tmp_path):
    pdf = _write(tmp_path, ELEMENT_CITATION)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    text = refs[1]["text"]
    assert "Read" in text
    assert "Improved Fourier coefficients" in text
    assert "1986" in text


# ---- robustness ------------------------------------------------------

def test_missing_or_unreadable_file_yields_nothing(tmp_path):
    assert jats.parse_ref_list(str(tmp_path / "nope.jats.xml")) == []
    bad = str(tmp_path / "bad.jats.xml")
    with open(bad, "w") as fh:
        fh.write("<article><back><ref-list>truncated")
    assert jats.parse_ref_list(bad) == []


def test_article_with_no_ref_list(tmp_path):
    pdf = _write(tmp_path, "<article><body>text</body></article>")
    assert jats.parse_ref_list(jats.jats_path(pdf)) == []


def test_unlabelled_refs_are_numbered_in_document_order(tmp_path):
    """Some publishers omit <label>; position is the number."""
    xml = ("<article><back><ref-list>"
           "<ref><element-citation><person-group>"
           "<name><surname>Alpha</surname></name></person-group>"
           "<year>2001</year></element-citation></ref>"
           "<ref><element-citation><person-group>"
           "<name><surname>Beta</surname></name></person-group>"
           "<year>2002</year></element-citation></ref>"
           "</ref-list></back></article>")
    pdf = _write(tmp_path, xml)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert [(r["n"], r["surname"]) for r in refs] == [
        (1, "Alpha"), (2, "Beta")]


def test_namespaced_jats_is_handled(tmp_path):
    """Europe PMC serves some articles with a default namespace."""
    xml = ('<article xmlns="http://jats.nlm.nih.gov"><back>'
           '<ref-list><ref><label>1.</label><element-citation>'
           '<person-group><name><surname>Gamma</surname></name>'
           '</person-group><year>1999</year></element-citation>'
           '</ref></ref-list></back></article>')
    pdf = _write(tmp_path, xml)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert len(refs) == 1
    assert refs[0]["surname"] == "Gamma"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ---- publishers encode the DOI in three different places -----------

EXT_LINK = ('<article><back><ref-list><ref id="CR1"><label>1.</label>'
            '<mixed-citation><named-content content-type='
            '"citation-string">Burley SK et al. Protein Data Bank. '
            'Nucleic Acids Res. 2019;47:D520.</named-content>'
            '<ext-link xmlns:xlink="http://www.w3.org/1999/xlink" '
            'ext-link-type="doi" xlink:href="10.1093/nar/gky949"/>'
            '<ext-link xmlns:xlink="http://www.w3.org/1999/xlink" '
            'ext-link-type="pmid" xlink:href="30357364"/>'
            '</mixed-citation></ref></ref-list></back></article>')


def test_doi_from_an_ext_link(tmp_path):
    """BMC/PMC style: no <pub-id>, the DOI is an <ext-link>. Reading
    only <pub-id> lost every DOI on those papers."""
    pdf = _write(tmp_path, EXT_LINK)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert refs[0]["doi"] == "10.1093/nar/gky949"


def test_pmid_ext_links_are_not_mistaken_for_dois(tmp_path):
    pdf = _write(tmp_path, EXT_LINK)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert refs[0]["doi"] != "30357364"


def test_doi_recovered_from_the_citation_string(tmp_path):
    """Last resort: some entries only print it in the text."""
    xml = ('<article><back><ref-list><ref><label>1.</label>'
           '<mixed-citation>Smith J. A paper. J Chem. 2020;1:1. '
           'doi: 10.1021/acs.jcim.0c01144.</mixed-citation>'
           '</ref></ref-list></back></article>')
    pdf = _write(tmp_path, xml)
    refs = jats.parse_ref_list(jats.jats_path(pdf))
    assert refs[0]["doi"] == "10.1021/acs.jcim.0c01144"


def test_a_trailing_full_stop_is_not_part_of_the_doi(tmp_path):
    xml = ('<article><back><ref-list><ref><label>1.</label>'
           '<mixed-citation>doi: 10.1093/nar/gky949.</mixed-citation>'
           '</ref></ref-list></back></article>')
    pdf = _write(tmp_path, xml)
    assert jats.parse_ref_list(
        jats.jats_path(pdf))[0]["doi"] == "10.1093/nar/gky949"
