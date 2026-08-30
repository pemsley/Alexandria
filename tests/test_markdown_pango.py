"""Summaries are written in Markdown; the card popover renders a
light subset of it as Pango markup.

Must never produce markup Pango rejects — a summary comes from a
model (or a person typing), so unbalanced or exotic input is
expected, and the fallback is plain escaped text rather than a
crash or a stray tag.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

markup = pytest.importorskip("alexandria.markup")
md = markup.markdown_to_pango


def _parses(s):
    return markup._markup_parses(s)


# ---- inline emphasis ----------------------------------------------

def test_bold():
    assert md("a **strong** claim") == "a <b>strong</b> claim"


def test_italic_with_asterisk_and_underscore():
    assert md("an *emphatic* point") == "an <i>emphatic</i> point"
    assert md("an _emphatic_ point") == "an <i>emphatic</i> point"


def test_inline_code():
    assert md("call `refmac5` here") == "call <tt>refmac5</tt> here"


def test_bold_wins_over_italic_for_double_markers():
    out = md("**both** and *one*")
    assert "<b>both</b>" in out and "<i>one</i>" in out


# ---- structure ----------------------------------------------------

def test_bullets_become_real_bullets():
    out = md("- first\n- second")
    assert "• first" in out and "• second" in out
    assert "- first" not in out


def test_asterisk_bullets_are_not_confused_with_italics():
    out = md("* first\n* second")
    assert "• first" in out and "• second" in out
    assert "<i>" not in out


def test_headings_become_bold_lines():
    out = md("## Methods\ntext")
    assert "<b>Methods</b>" in out
    assert "#" not in out


def test_paragraph_breaks_are_preserved():
    assert "\n\n" in md("one\n\ntwo")


# ---- safety --------------------------------------------------------

def test_escapes_markup_characters():
    out = md("5 < 6 & 7 > 2")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out
    assert _parses(out)


def test_angle_brackets_in_text_do_not_become_tags():
    out = md("the <script>alert</script> tag")
    assert "<script>" not in out
    assert _parses(out)


def test_unbalanced_markers_are_left_alone():
    for s in ("a ** dangling", "one * star", "back ` tick"):
        out = md(s)
        assert _parses(out), "unparseable for {!r}: {}".format(s, out)


def test_empty_and_none():
    assert md("") == ""
    assert md(None) == ""


def test_output_always_parses_for_awkward_input():
    awkward = [
        "**bold with `code` inside**",
        "_italic_ and **bold** and `tt` together",
        "* bullet with **bold**",
        "### heading with *emphasis*",
        "a ***triple*** marker",
        "100% & <tags> everywhere",
    ]
    for s in awkward:
        assert _parses(md(s)), "unparseable for {!r}".format(s)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
