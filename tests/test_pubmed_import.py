"""Import by PubMed / PMC identifier, beside import by DOI.

Europe PMC answers both directions of the same query we already use
for JATS, so resolving an identifier to a DOI hands straight off to
the existing DOI import path.

Two things learned from the live API on 2026-09-04, both of which
would otherwise be silent no-results:

  * The field queries must be *unquoted*. `PMCID:PMC5336473` works,
    `PMCID:"PMC5336473"` returns nothing — unlike `DOI:"..."`, which
    needs its quotes and is what `pmcid_for_doi` already sends.
  * A bare number is not ambiguous. PMCIDs always carry the `PMC`
    prefix, so a naked integer is a PMID.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import jats


# ---- what the user might paste --------------------------------------

def test_a_pmc_identifier():
    assert jats.parse_pubmed_identifier("PMC5336473") == \
        ("pmcid", "PMC5336473")


def test_a_pmc_identifier_in_any_case_or_spacing():
    for s in ("pmc5336473", "  PMC 5336473 ", "PMCID: PMC5336473",
              "pmcid:pmc5336473"):
        assert jats.parse_pubmed_identifier(s) == ("pmcid", "PMC5336473"), s


def test_a_bare_number_is_a_pmid():
    """PMCIDs are always PMC-prefixed, so an unprefixed number can
    only be a PubMed ID — no need to ask the user which they meant."""
    assert jats.parse_pubmed_identifier("28345007") == ("pmid", "28345007")


def test_an_explicit_pmid():
    for s in ("PMID:28345007", "pmid 28345007", "PMID: 28345007"):
        assert jats.parse_pubmed_identifier(s) == ("pmid", "28345007"), s


def test_urls_pasted_from_a_browser():
    for s, want in (
            ("https://pubmed.ncbi.nlm.nih.gov/28345007/",
             ("pmid", "28345007")),
            ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5336473/",
             ("pmcid", "PMC5336473")),
            ("https://europepmc.org/article/MED/28345007",
             ("pmid", "28345007")),
            ("https://europepmc.org/article/PMC/PMC5336473",
             ("pmcid", "PMC5336473"))):
        assert jats.parse_pubmed_identifier(s) == want, s


# ---- what is not one ------------------------------------------------

def test_a_doi_is_not_a_pubmed_identifier():
    """The dialog tries DOI first; this must not claim one."""
    assert jats.parse_pubmed_identifier("10.1063/1.4974176") is None
    assert jats.parse_pubmed_identifier(
        "https://doi.org/10.1063/1.4974176") is None


def test_nonsense_and_emptiness():
    for s in ("", "   ", None, "hello", "PMC", "PMCabc", "12.34"):
        assert jats.parse_pubmed_identifier(s) is None, repr(s)


def test_an_implausibly_long_number_is_not_a_pmid():
    """A stray year range or a phone number should not send us to
    the API."""
    assert jats.parse_pubmed_identifier("1" * 12) is None


# ---- the query the identifier turns into -----------------------------

def test_the_pmcid_query_is_unquoted():
    """`PMCID:"PMC5336473"` returns nothing from Europe PMC; the
    unquoted form works. Easy to 'tidy' into a silent no-result."""
    q = jats.pubmed_query("pmcid", "PMC5336473")
    assert q == "PMCID:PMC5336473"
    assert '"' not in q


def test_the_pmid_query_names_the_source():
    q = jats.pubmed_query("pmid", "28345007")
    assert "EXT_ID:28345007" in q
    assert "SRC:MED" in q
    assert '"' not in q


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
