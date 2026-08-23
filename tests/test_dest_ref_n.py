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


# --- OUP / 3B2 `-B<N>` shape ------------------------------------------
#
# Oxford University Press exports (NAR gkr900, PNAS Nexus pgag197)
# name bibliography dests "<article-id>-B<N>" and figure / table
# dests "-F<N>" / "-T<N>".

def test_oup_bibliography_dest():
    assert L._ref_n_from_dest_name("WEBgkr900-B1") == 1
    assert L._ref_n_from_dest_name("WEBgkr900-B12") == 12
    assert L._ref_n_from_dest_name("WEBgkr900-B17") == 17


def test_oup_figure_and_table_dests_are_none():
    # Same document, same naming scheme — these must not raise a
    # reference popover.
    assert L._ref_n_from_dest_name("WEBgkr900-F1") is None
    assert L._ref_n_from_dest_name("WEBgkr900-T2") is None


def test_oup_pattern_is_anchored_at_the_end():
    # `-B<N>` only counts as the whole tail of the name, so a `-B12`
    # buried mid-name isn't mistaken for an entry number.
    assert L._ref_n_from_dest_name("doc-B12-figure") is None
    assert L._ref_n_from_dest_name("sectionB4") is None


# --- _dest_top fit modes ----------------------------------------------

def test_dest_top_xyz_and_fith():
    assert L._dest_top([None, "/XYZ", 100, 530, 0]) == 530.0
    assert L._dest_top([None, "/FitH", 530]) == 530.0


def test_dest_top_fitr_uses_the_top_slot():
    # /FitR left bottom right top -> the 4th argument. OUP's 3B2
    # emits the pair reversed (bottom > top); take the slot the spec
    # names rather than max(), which lands ~9pt too high and pushes
    # the entry outside assign_ref_n_by_position's 12pt tolerance.
    assert L._dest_top([None, "/FitR", 262, 543, 605, 534]) == 534.0
    assert L._dest_top([None, "/FitR", 275, 676, 613, 667]) == 667.0


def test_dest_top_fitr_short_array_is_none():
    assert L._dest_top([None, "/FitR", 262, 543]) is None


def test_dest_top_modes_without_a_top():
    assert L._dest_top([None, "/Fit"]) is None
    assert L._dest_top([None, "/FitV", 100]) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
