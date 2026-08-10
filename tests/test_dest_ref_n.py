"""Tests for pdf_links._ref_n_from_dest_name — reference-number
extraction from named-destination strings.

Two publisher shapes carry the entry number:

  * classic Springer/Nature `CR<N>` names
    ("13321_2024_821_Article.indd:CR12:103", "bm_CR1"), and

  * the 2025-era springernature InDesign export, where the dest name
    embeds the *entire reference text* prefixed by the entry marker,
    with a variable number of U+FEFF (BOM) characters sprinkled in:
    "springernature_nature_9761.indd:<BOM>1.<BOM><BOM>\t<BOM>Kirsch,
    L., …:246". The BOM count varies between links pointing at the
    same entry (one, two and three BOMs are all observed in the same
    document). In the tests below the BOM appears as the `B` constant
    so no invisible characters live in the source.

Figure / table cross-reference dests from the same exporter
("…indd:<BOM><BOM><BOM>Fig. 1<BOM>…") must NOT yield a number — a
None ref_n is what tells the viewer to skip the reference popover
for them.

Runnable as `python3 -m tests.test_dest_ref_n` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import pdf_links as L

B = "\ufeff"  # U+FEFF; kept out of literals so editors can't silently eat it


# --- classic CR<N> shapes keep working --------------------------------

def test_cr_springer_indesign():
    assert L._ref_n_from_dest_name(
        "13321_2024_821_Article.indd:CR12:103") == 12


def test_cr_nature_bookmark():
    assert L._ref_n_from_dest_name("bm_CR1") == 1


def test_cr_substring_of_word_does_not_match():
    assert L._ref_n_from_dest_name("SCR12_section") is None


# --- springernature .indd embedded-text shape -------------------------

def test_indd_single_bom():
    name = (f"springernature_nature_9761.indd:{B}1.{B}{B}\t{B}{B}"
            "Kirsch, L., van Steenkiste, S. & Schmidhuber, J. …:246")
    assert L._ref_n_from_dest_name(name) == 1


def test_indd_double_bom():
    name = (f"springernature_nature_9761.indd:{B}{B}6.{B}{B}\t{B}{B}"
            "Lu, C. et al. Discovered policy optimisation. …")
    assert L._ref_n_from_dest_name(name) == 6


def test_indd_triple_bom():
    name = (f"springernature_nature_9761.indd:{B}{B}{B}1.{B}{B}\t{B}"
            "Kirsch, L., van Steenkiste, S. …")
    assert L._ref_n_from_dest_name(name) == 1


def test_indd_two_digit_entry():
    name = (f"springernature_nature_9761.indd:{B}46.{B}{B}\t{B}"
            "Storck, J., et al. …")
    assert L._ref_n_from_dest_name(name) == 46


def test_indd_figure_dest_is_none():
    name = (f"springernature_nature_9761.indd:{B}{B}{B}Fig. 1{B}{B} | "
            f"{B}{B}{B}Discovering …")
    assert L._ref_n_from_dest_name(name) is None


def test_indd_year_like_number_is_none():
    # A 4-digit "marker" is not a bibliography entry number — don't
    # let a year that happens to lead the embedded text match.
    assert L._ref_n_from_dest_name(f"x.indd:{B}2019.{B}Some text") is None


def test_opaque_names_are_none():
    assert L._ref_n_from_dest_name("Anchor 12") is None
    assert L._ref_n_from_dest_name("section-4.2") is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
