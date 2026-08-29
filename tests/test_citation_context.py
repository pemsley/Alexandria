"""Tests for references_pdf.citation_context — the sentence a citation
appears in, shown in the reference popover as "Cited here as:".

This answers "how did *this* paper characterise the work it is
citing?", which is a different question from Semantic Scholar's
citation contexts ("how does the field characterise it?") and needs no
network at all: the sentence is on the page already being read.

The pure helpers are tested directly. The rect->offset mapping needs a
real Poppler page, so it's covered by the end-to-end checks rather than
here.

Runnable as `python3 -m tests.test_citation_context` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import references_pdf as R


# ---- Sentence bounds ----

def _around(text, needle):
    i = text.index(needle)
    lo, hi = R._sentence_bounds(text, i, i + len(needle))
    return text[lo:hi].strip()


def test_picks_the_containing_sentence():
    t = ("Something earlier happened. Most non-Africans possess "
         "Neanderthal ancestry (6, 7). A later sentence follows.")
    assert _around(t, "(6, 7)").startswith("Most non-Africans")
    assert _around(t, "(6, 7)").endswith("(6, 7).")


def test_initials_do_not_end_a_sentence():
    # "J. Smith" must not be read as a sentence break, or the context
    # starts mid-name — reference-dense text is full of initials.
    t = "As shown by J. Smith and co-workers, the effect is large (4)."
    assert _around(t, "(4)").startswith("As shown by J. Smith")


def test_closing_quote_or_bracket_after_the_stop():
    t = 'He called it "settled." The result was later revised (9).'
    assert _around(t, "(9)").startswith("The result")


def test_backward_search_is_capped():
    # Without a cap, a failed search walks to the top of the page and
    # drags in the author/affiliation block.
    t = "x" * 2000 + " the claim holds (3)."
    got = _around(t, "(3)")
    assert len(got) < 600, len(got)


def test_forward_search_is_capped():
    t = "The claim holds (3)" + " y" * 2000
    lo, hi = R._sentence_bounds(t, t.index("(3)"), t.index("(3)") + 3)
    assert hi - lo <= R._CONTEXT_MAX_BACK + R._CONTEXT_MAX_FWD + 10


# ---- Front matter ----

def test_strips_a_correspondence_block():
    s = ("Email: someone@example.edu (A.B.); other@example.edu (C.D.) "
         "The sequencing of the genome transformed the field (1-5).")
    out = R._strip_front_matter(s)
    assert out.startswith("The sequencing"), out


def test_strips_corresponding_author_wording():
    s = ("*Corresponding author. Reported values are means (7).")
    assert R._strip_front_matter(s).startswith("Reported values"), \
        R._strip_front_matter(s)


def test_ordinary_sentences_are_untouched():
    s = "Most non-Africans possess Neanderthal ancestry (6, 7)."
    assert R._strip_front_matter(s) == s


def test_no_word_start_after_marker_keeps_the_original():
    # Don't return an empty string just because a marker was seen.
    s = "contact@example.edu"
    assert R._strip_front_matter(s) == s


# ---- Whitespace / hyphenation tidying ----

def test_joins_hyphenated_line_breaks():
    assert R._tidy_context("intro-\ngressed segments") == "introgressed segments"


def test_collapses_whitespace():
    assert R._tidy_context("a   b\n c") == "a b c"


def test_joins_hyphen_split_across_spaces():
    assert R._tidy_context("atmos- pheres") == "atmospheres"


# ---- Self-test runner ----

def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print("FAIL  {}\n        {}".format(t.__name__, e))
        except Exception as e:
            failures += 1
            print("ERROR {}\n        {!r}".format(t.__name__, e))
        else:
            print("ok    {}".format(t.__name__))
    print()
    print("{} test(s), {} failure(s)".format(len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
