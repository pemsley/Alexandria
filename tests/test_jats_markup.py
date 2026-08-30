"""Crossref/OpenAlex abstracts arrive as namespaced JATS fragments
— `<jats:p>`, `<jats:italic>`, `<jats:sub>` — and titles carry bare
`<sup>` / `<scp>`. Feed cards were escaping all of it, so readers
saw the tags instead of the formatting.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

markup = pytest.importorskip("alexandria.markup")
safe = markup.safe_pango_markup


def _parses(s):
    return markup._markup_parses(s)


def test_jats_italic_becomes_italic():
    out = safe("in <jats:italic>Oryza sativa</jats:italic> plants")
    assert "<i>Oryza sativa</i>" in out
    assert "jats" not in out


def test_jats_sub_and_sup():
    out = safe("NH<jats:sub>2</jats:sub> and Ca<jats:sup>2+</jats:sup>")
    assert "<sub>2</sub>" in out
    assert "<sup>2+</sup>" in out


def test_jats_bold_and_monospace():
    out = safe("<jats:bold>key</jats:bold> and "
               "<jats:monospace>refmac</jats:monospace>")
    assert "<b>key</b>" in out
    assert "<tt>refmac</tt>" in out


def test_jats_paragraph_tags_are_dropped_not_shown():
    out = safe("<jats:p>First sentence.</jats:p>")
    assert "jats:p" not in out
    assert "First sentence." in out


def test_two_jats_paragraphs_stay_separated():
    out = safe("<jats:p>One.</jats:p><jats:p>Two.</jats:p>")
    assert "One." in out and "Two." in out
    # Not run together into "One.Two."
    assert "One.Two." not in out


def test_unknown_jats_tags_are_dropped_keeping_their_text():
    out = safe("<jats:sec><jats:title>Methods</jats:title>"
               "text</jats:sec>")
    assert "Methods" in out and "text" in out
    assert "jats:" not in out


def test_bare_sup_in_a_title_is_rendered():
    out = safe("recognition by CRL2<sup>ZER1</sup>")
    assert "<sup>ZER1</sup>" in out


def test_small_caps_still_work():
    out = safe("Crystal structure of rice <scp>L</scp>-galactose")
    assert "smallcaps" in out
    assert "scp" not in out.replace("smallcaps", "")


def test_stray_angle_brackets_still_escaped():
    out = safe("5 < 6 and <notatag>x</notatag>")
    assert "&lt;" in out
    assert _parses(out)


def test_output_parses_for_awkward_jats():
    awkward = [
        "<jats:p>a <jats:italic>b</jats:p>",          # unbalanced
        "<jats:italic>only an opener",
        "</jats:italic> only a closer",
        "<jats:p>100% &amp; <jats:sub>x</jats:sub></jats:p>",
        "text with a bare & ampersand",
    ]
    for s in awkward:
        assert _parses(safe(s)), "unparseable for {!r}".format(s)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---- source whitespace (Crossref returns pretty-printed XML) -------

REAL_TITLE = ('Crystal structure of rice\n                    '
              '<scp>L</scp>\n                    '
              '-galactose dehydrogenase')
REAL_ABSTRACT = ('<jats:p>\n                    L-Galactose plays a '
                 'role in rice (\n                    '
                 '<jats:italic>Oryza sativa</jats:italic>\n'
                 '                    ) leaves.</jats:p>')


def test_indented_title_collapses_to_one_line():
    out = safe(REAL_TITLE)
    assert "\n" not in out
    assert "                " not in out


def test_title_keeps_the_word_space_but_not_the_hyphen_gap():
    out = safe(REAL_TITLE)
    assert "rice <span" in out                  # space before small-caps L
    assert "</span>-galactose" in out           # but not before the hyphen


def test_abstract_bracket_spacing_is_tidied():
    out = safe(REAL_ABSTRACT)
    assert "rice (<i>Oryza sativa</i>) leaves." in out


def test_paragraph_breaks_survive_whitespace_collapsing():
    out = safe("<jats:p>One.</jats:p>\n  <jats:p>Two.</jats:p>")
    assert "One." in out and "Two." in out
    assert "One. Two." not in out


def test_no_leading_space_where_a_dropped_tag_stood():
    out = safe("<jats:p> Text begins here.</jats:p>")
    assert not out.startswith(" ")
    assert out.startswith("Text begins here.")
