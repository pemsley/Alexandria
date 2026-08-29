"""Citation disambiguation: parse_citation_hint ("Jones et al. JMB,
1995" -> surname/year/journal) and find_citation_candidates (ranked
OpenAlex candidate list with the exact -> [y-1,y] -> no-year
ladder). Network is canned via metrics._http_get_json."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


# ---- parse_citation_hint ------------------------------------------

def test_parse_et_al_comma_year():
    got = metrics.parse_citation_hint("Jones et al. JMB, 1995")
    assert got == {"surname": "Jones", "year": 1995, "journal": "JMB"}


def test_parse_et_al_dotted_journal():
    got = metrics.parse_citation_hint(
        "Jones et al., J. Mol. Biol., 1995")
    assert got["surname"] == "Jones"
    assert got["year"] == 1995
    assert got["journal"] == "J. Mol. Biol."


def test_parse_single_author_sections():
    got = metrics.parse_citation_hint("Read, Acta Cryst. A, 1986")
    assert got == {"surname": "Read", "year": 1986,
                   "journal": "Acta Cryst. A"}


def test_parse_paren_year_no_commas():
    got = metrics.parse_citation_hint("Sheldrick (2008) Acta Cryst A")
    assert got == {"surname": "Sheldrick", "year": 2008,
                   "journal": "Acta Cryst A"}


def test_parse_year_glued_to_journal():
    got = metrics.parse_citation_hint("Jones et al. JMB1995")
    assert got == {"surname": "Jones", "year": 1995, "journal": "JMB"}


def test_parse_year_not_taken_from_long_digit_runs():
    # DOIs / PIIs contain year-like substrings inside digit runs.
    got = metrics.parse_citation_hint(
        "Smith, S2059798319011471, 2020")
    assert got["year"] == 2020


def test_parse_multi_author_ampersand():
    got = metrics.parse_citation_hint(
        "Jones, Willett & Glen, JMB 1995")
    assert got["surname"] == "Jones"
    assert got["year"] == 1995
    assert got["journal"] == "JMB"


def test_parse_skips_initials_for_surname():
    got = metrics.parse_citation_hint("R. J. Read, Acta Cryst. A, 1986")
    assert got["surname"] == "Read"


def test_parse_empty_and_garbage():
    assert metrics.parse_citation_hint("") == {
        "surname": None, "year": None, "journal": None}
    got = metrics.parse_citation_hint("1995")
    assert got["year"] == 1995
    assert got["surname"] is None


# ---- find_citation_candidates -------------------------------------

def _work(doi, first, cites, year=1995,
          journal="Journal of Molecular Biology", extra_authors=()):
    auths = [{"author_position": "first",
              "author": {"display_name": first}}]
    for n in extra_authors:
        auths.append({"author_position": "middle",
                      "author": {"display_name": n}})
    return {
        "id": "https://openalex.org/W{}".format(abs(hash(doi)) % 10**8),
        "doi": "https://doi.org/" + doi,
        "title": "Paper " + doi,
        "publication_year": year,
        "cited_by_count": cites,
        "authorships": auths,
        "primary_location": {"source": {"display_name": journal}},
    }


def _install(payload_by_yearfilt):
    """metrics._http_get_json fake keyed on the publication_year
    filter present in the URL ('exact', 'minus1', 'none')."""
    saved = metrics._http_get_json
    calls = []

    def fake(url, headers, timeout, raise_on_quota=False):
        calls.append(url)
        if "publication_year%3A1995%7C" in url or \
           "publication_year:1995|" in url:
            key = "minus1"
        elif "publication_year" in url:
            key = "exact"
        else:
            key = "none"
        return payload_by_yearfilt.get(key)

    metrics._http_get_json = fake
    return (lambda: setattr(metrics, "_http_get_json", saved)), calls


def test_candidates_first_author_outranks_citations():
    gold = _work("10.1016/gold", "Gareth Jones", 1548)
    kung = _work("10.1165/kung", "T T Kung", 1900,
                 extra_authors=("B. Jones",))
    restore, _ = _install({"exact": {"results": [kung, gold]}})
    try:
        mode, cands = metrics.find_citation_candidates(
            "Jones", 1996, journal=None)
    finally:
        restore()
    assert mode == "exact"
    assert [c["doi"] for c in cands] == ["10.1016/gold", "10.1165/kung"]
    assert cands[0]["first_author"] == "Gareth Jones"
    assert cands[0]["cited_by_count"] == 1548


def test_candidates_year_ladder_falls_back():
    w = _work("10.1/x", "Gareth Jones", 10)
    restore, calls = _install({
        "exact": {"results": []},
        "minus1": {"results": [w]},
    })
    try:
        mode, cands = metrics.find_citation_candidates("Jones", 1996)
    finally:
        restore()
    assert mode == "minus1"
    assert len(cands) == 1
    assert len(calls) == 2


def test_candidates_shape():
    w = _work("10.1/x", "Randy J. Read", 1885, year=1986,
              journal="Acta Crystallographica Section A",
              extra_authors=("A. N. Other",))
    restore, _ = _install({"exact": {"results": [w]}})
    try:
        _mode, cands = metrics.find_citation_candidates("Read", 1986)
    finally:
        restore()
    c = cands[0]
    assert c["doi"] == "10.1/x"
    assert c["title"] == "Paper 10.1/x"
    assert c["year"] == 1986
    assert c["journal"] == "Acta Crystallographica Section A"
    assert c["authors"] == ["Randy J. Read", "A. N. Other"]
    assert c["openalex_id"].startswith("W")


def test_candidates_drop_poisoned_journal_filter():
    """A journal hint that resolves to the WRONG OpenAlex source
    ('JMB' -> J. Microbiology & Biotech.) must not zero the search:
    when every source-filtered rung is empty, retry without it."""
    w = _work("10.1016/gold", "Gareth Jones", 1548)
    saved_resolve = metrics._resolve_journal_source_ids
    metrics._resolve_journal_source_ids = lambda j: ["S999"]
    saved = metrics._http_get_json
    calls = []

    def fake(url, headers, timeout, raise_on_quota=False):
        calls.append(url)
        if "primary_location.source.id" in urllib_unquote(url):
            return {"results": []}
        return {"results": [w]}

    def urllib_unquote(u):
        import urllib.parse
        return urllib.parse.unquote(u)

    metrics._http_get_json = fake
    try:
        mode, cands = metrics.find_citation_candidates(
            "Jones", 1995, journal="JMB")
    finally:
        metrics._http_get_json = saved
        metrics._resolve_journal_source_ids = saved_resolve
    assert cands and cands[0]["doi"] == "10.1016/gold"
    assert mode == "exact"


def test_journal_hint_match_initialism():
    assert metrics._journal_hint_match(
        "JMB", "Journal of Molecular Biology") is True
    assert metrics._journal_hint_match(
        "JMB", "Harvard business review") is False
    assert metrics._journal_hint_match(
        "PNAS",
        "Proceedings of the National Academy of Sciences") is True


def test_journal_hint_match_delegates_token_match():
    assert metrics._journal_hint_match(
        "Acta Cryst. A",
        "Acta Crystallographica Section A Foundations") is True


def test_candidates_soft_journal_ranking_in_fallback():
    """When the journal source filter was dropped (wrong resolution),
    a first-author candidate whose journal matches the hint must
    outrank a higher-cited first-author candidate that doesn't."""
    hbr = _work("10.1/hbr", "Thomas O. Jones", 1953,
                journal="Harvard business review")
    gold = _work("10.1016/gold", "Gareth Jones", 1548,
                 journal="Journal of Molecular Biology")
    saved_resolve = metrics._resolve_journal_source_ids
    metrics._resolve_journal_source_ids = lambda j: []
    restore, _ = _install({"exact": {"results": [hbr, gold]}})
    try:
        _mode, cands = metrics.find_citation_candidates(
            "Jones", 1995, journal="JMB")
    finally:
        restore()
        metrics._resolve_journal_source_ids = saved_resolve
    assert [c["doi"] for c in cands] == ["10.1016/gold", "10.1/hbr"]


def test_candidates_no_surname_returns_empty():
    assert metrics.find_citation_candidates(None, 1995) == (None, [])


def test_candidates_max_n_cap():
    works = [_work("10.1/n{}".format(i), "A. Jones", 100 - i)
             for i in range(15)]
    restore, _ = _install({"exact": {"results": works}})
    try:
        _mode, cands = metrics.find_citation_candidates(
            "Jones", 1995, max_n=5)
    finally:
        restore()
    assert len(cands) == 5


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
