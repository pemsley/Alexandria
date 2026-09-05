"""A corporate author is one name, not a person to be reordered.

Found 2026-09-05 in a real export. The input had

    author = {{Meta Platforms, Inc.}}

— double-braced, which is BibTeX for "this is a single name, do not
parse it into first and last". Alexandria stripped the inner braces,
read it as a personal name, decided the surname was "Meta", and
wrote back

    author = {Platforms, Inc. Meta}

which renders as nonsense in a bibliography. Same family as the
`\\url{…}` and `{ATP}` losses: braces discarded without asking what
they were protecting.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import bibtex

SRC = ("@misc{react,\n"
       "  author = {{Meta Platforms, Inc.}},\n"
       "  title  = {React},\n"
       "  year   = {2013}\n}\n")


# ---- reading one -----------------------------------------------------

def test_a_corporate_author_stays_whole():
    rec = bibtex.parse(SRC)[0]
    assert rec["authors"] == ["Meta Platforms, Inc."]


def test_it_is_not_reordered_into_first_last():
    rec = bibtex.parse(SRC)[0]
    assert rec["authors"][0] != "Platforms, Inc. Meta"
    assert not rec["authors"][0].startswith("Platforms")


def test_a_corporate_author_beside_people():
    src = ("@misc{k, author = {{The CCP4 Consortium} and Smith, Jane},\n"
           "  title = {T}, year = {2020}}\n")
    assert bibtex.parse(src)[0]["authors"] == [
        "The CCP4 Consortium", "Jane Smith"]


def test_ordinary_authors_are_still_reordered():
    """The whole point of the display form — this must not regress."""
    src = "@misc{k, author = {Emsley, Paul}, title = {T}, year={2020}}"
    assert bibtex.parse(src)[0]["authors"] == ["Paul Emsley"]


# ---- writing one back ------------------------------------------------

def test_it_is_written_back_double_braced():
    """Without the braces the next reader makes the same mistake."""
    rec = bibtex.parse(SRC)[0]
    out = bibtex.write([rec])
    assert "{{Meta Platforms, Inc.}}" in out


def test_it_survives_a_round_trip():
    rec = bibtex.parse(SRC)[0]
    back = bibtex.parse(bibtex.write([rec]))[0]
    assert back["authors"] == ["Meta Platforms, Inc."]


def test_a_person_is_not_double_braced():
    src = "@misc{k, author = {Emsley, Paul}, title = {T}, year={2020}}"
    out = bibtex.write([bibtex.parse(src)[0]])
    assert "{Emsley, Paul}" in out
    assert "{{" not in out


def test_a_mixed_list_round_trips():
    src = ("@misc{k, author = {{The CCP4 Consortium} and Smith, Jane},\n"
           "  title = {T}, year = {2020}}\n")
    back = bibtex.parse(bibtex.write([bibtex.parse(src)[0]]))[0]
    assert back["authors"] == ["The CCP4 Consortium", "Jane Smith"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
