"""The BibTeX citation key as an editable field.

A citation key is a human artefact — people have typed
`emsley2010features` into LaTeX for decades, and a paper's key often
has to match one already used in a group's `.bib` file or a
collaborator's manuscript. Alexandria could only ever autogenerate
one at export time, so a key you had already committed to could not
be recorded.

The generator that export has used all along is promoted to a public
`suggest_key`, so the dialog's Suggest button and the export fallback
cannot drift apart.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

bibtex_export = pytest.importorskip("alexandria.bibtex_export")


REC = {"title": "Features and development of Coot",
       "authors": ["Paul Emsley", "Bernhard Lohkamp"],
       "year": 2010, "journal": "Acta Cryst D"}


# ---- suggesting a key ------------------------------------------------

def test_the_shape_is_surname_year_titleword():
    assert bibtex_export.suggest_key(REC) == "emsley2010features"


def test_leading_stopwords_in_the_title_are_skipped():
    rec = dict(REC, title="The structure of the ribosome")
    assert bibtex_export.suggest_key(rec) == "emsley2010structure"


def test_a_missing_year_still_gives_a_usable_key():
    rec = dict(REC)
    rec.pop("year")
    assert bibtex_export.suggest_key(rec) == "emsleynodatefeatures"


def test_no_authors_falls_back_rather_than_failing():
    rec = dict(REC, authors=[])
    assert bibtex_export.suggest_key(rec).startswith("anon")


def test_an_empty_record_still_returns_something():
    """The Suggest button must always produce a key — an empty
    entry box would look broken."""
    assert bibtex_export.suggest_key({})
    assert bibtex_export.suggest_key(None)


def test_diacritics_and_punctuation_are_stripped():
    """A key with a hyphen or an accent is not portable through
    every LaTeX toolchain."""
    rec = dict(REC, authors=["Jesús Jiménez-Barbero"])
    key = bibtex_export.suggest_key(rec)
    assert key.isascii() and key.isalnum()


def test_export_still_uses_the_same_generator(tmp_path):
    """Suggest and the export fallback must agree, or a key you
    accepted in the dialog would differ from the one in the .bib."""
    r = bibtex_export.sidecar_to_bibtex_record(dict(REC))
    assert r["bibtex_key"] == bibtex_export.suggest_key(REC)


def test_a_stored_key_beats_the_suggestion():
    r = bibtex_export.sidecar_to_bibtex_record(
        dict(REC, bibtex_key="Emsley2010-coot"))
    assert r["bibtex_key"] == "Emsley2010-coot"


# ---- keeping a typed key usable --------------------------------------

def test_spaces_are_not_allowed_in_a_key():
    """BibTeX ends the key at the first comma or brace, and a space
    breaks most parsers. A typed key has to be cleaned."""
    assert bibtex_export.sanitise_key("emsley 2010 features") == \
        "emsley2010features"


def test_the_characters_bibtex_reserves_are_removed():
    assert bibtex_export.sanitise_key("a{b}c,d") == "abcd"


def test_hyphens_and_colons_survive():
    """Widely used in hand-written keys, and accepted by BibTeX and
    biblatex alike."""
    assert bibtex_export.sanitise_key("Emsley2010-coot:v2") == \
        "Emsley2010-coot:v2"


def test_case_is_preserved():
    """Unlike the suggestion, a typed key is the user's to shape —
    plenty of groups capitalise."""
    assert bibtex_export.sanitise_key("Emsley2010") == "Emsley2010"


def test_an_empty_or_blank_key_sanitises_to_empty():
    assert bibtex_export.sanitise_key("") == ""
    assert bibtex_export.sanitise_key("   ") == ""
    assert bibtex_export.sanitise_key(None) == ""


def test_a_key_of_only_illegal_characters_is_empty_not_garbage():
    assert bibtex_export.sanitise_key("{},") == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
