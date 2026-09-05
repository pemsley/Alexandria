"""Everything written must read back identically.

Found 2026-09-02 by round-tripping a real 76-entry references.bib:
two abstracts came back about a third of their original length. Both
truncated at a per-cent sign — `%` starts a comment, and the writer
was emitting it raw, so everything after it on the line was lost. The
line is the whole field, so an abstract mentioning "78% sequence
identity" silently lost its second half.

This is export data loss, not just a parser quirk: a .bib we hand to
someone is read by their tools too.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

bibtex = pytest.importorskip("alexandria.bibtex")


def _round_trip(**extra):
    rec = {"bibtex_key": "k1", "bibtex_type": "article",
           "title": "A Title", "authors": ["Jane Smith"], "year": 2020,
           "bibtex_extra": dict(extra)}
    back = bibtex.parse(bibtex.write([rec]))
    assert len(back) == 1, "the entry itself must survive"
    return back[0]


def test_a_per_cent_sign_survives_a_round_trip():
    got = _round_trip(abstract="Before 90% after.")
    assert got["bibtex_extra"]["abstract"] == "Before 90% after."


def test_the_text_after_a_per_cent_sign_is_not_lost():
    """The failure mode that started this: silent truncation, not an
    error, so nobody notices until the abstract looks short."""
    long_tail = "x" * 200
    got = _round_trip(abstract="78% identity " + long_tail)
    assert got["bibtex_extra"]["abstract"].endswith(long_tail)


def test_several_per_cent_signs():
    got = _round_trip(abstract="10% and 20% and 30%")
    assert got["bibtex_extra"]["abstract"] == "10% and 20% and 30%"


def test_a_per_cent_sign_in_a_title():
    rec = {"bibtex_key": "k1", "bibtex_type": "article",
           "title": "Coverage of 95% of cases", "authors": ["A B"],
           "year": 2020}
    assert bibtex.parse(bibtex.write([rec]))[0]["title"] == \
        "Coverage of 95% of cases"


def test_an_already_escaped_per_cent_is_not_double_escaped():
    """A value that came from a LaTeX-aware source may already carry
    `\\%`; writing it must not turn it into `\\\\%`."""
    got = _round_trip(note=r"Exactly 50\% here")
    assert got["bibtex_extra"]["note"].count("%") == 1
    assert "\\\\" not in got["bibtex_extra"]["note"]


def test_the_whole_entry_still_parses_after_a_per_cent():
    """Truncation ate the closing brace too, so the damage was not
    confined to the one field."""
    got = _round_trip(abstract="50% lost", note="kept")
    assert got["bibtex_extra"].get("note") == "kept"
    assert got["title"] == "A Title"


# ---- the fields that were never at risk, guarded anyway -------------

def test_a_page_range_keeps_its_bibtex_spelling():
    """The parser normalises `123--130` to an en dash, because
    `ris_export._split_pages` and `csl._normalise_pages` rely on it.
    The BibTeX writer has to put `--` back, or every export silently
    rewrites the page ranges — 35 of the 53 in a real file."""
    rec = {"bibtex_key": "k1", "bibtex_type": "article",
           "title": "T", "authors": ["A B"], "year": 2020,
           "bibtex_extra": {"pages": "123--130"}}
    out = bibtex.write([rec])
    assert "{123--130}" in out and "123–130" not in out


def test_pages_and_volume_survive():
    got = _round_trip(pages="123--130", volume="66", number="4")
    assert got["bibtex_extra"]["pages"] == "123–130"
    assert got["bibtex_extra"]["volume"] == "66"
    assert got["bibtex_extra"]["number"] == "4"


def test_a_url_with_a_query_string_survives():
    got = _round_trip(url="https://example.org/a?b=1&c=2")
    assert got["bibtex_extra"]["url"] == "https://example.org/a?b=1&c=2"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
