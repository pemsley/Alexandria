"""The never-shrink rule for OpenAlex author enrichment: OpenAlex
work records are sometimes truncated (W2097382368 lists one author
for a five-author 1997 paper), and enrichment must not replace a
longer author list we already hold with a shorter one.

Runnable as `python3 -m tests.test_oa_author_names` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics, bibtex_import


def _aships(*names):
    return [{"name": n, "position": "middle", "orcid": None,
             "openalex_id": "A1", "institution": None} for n in names]


def test_replaces_when_oa_has_more():
    out = metrics.oa_author_names(
        _aships("A", "B", "C"), existing=["A B"])
    assert out == ["A", "B", "C"]


def test_keeps_existing_when_oa_shrinks():
    assert metrics.oa_author_names(
        _aships("Julie Thompson"),
        existing=["Julie D. Thompson", "Toby J. Gibson",
                  "F. Plewniak", "F. Jeanmougin"]) is None


def test_equal_length_replaces():
    # Same count: OA names are cleaner (no extraction warts) — take them.
    assert metrics.oa_author_names(
        _aships("A", "B"), existing=["A x", "B y"]) == ["A", "B"]


def test_no_existing_list_takes_oa():
    assert metrics.oa_author_names(_aships("A"), existing=None) == ["A"]
    assert metrics.oa_author_names(_aships("A"), existing=[]) == ["A"]


def test_empty_authorships_none():
    assert metrics.oa_author_names([], existing=["A"]) is None
    assert metrics.oa_author_names(None, existing=None) is None


def test_enrich_with_openalex_respects_guard(monkeypatch):
    # End-to-end through bibtex_import._enrich_with_openalex with a
    # canned fetch_metrics: one OA authorship must not shrink four.
    def fake_fetch(doi):
        return (39281, "openalex", [], None,
                _aships("Julie Thompson"), [],
                "The CLUSTAL_X windows interface", 1997,
                True, "bronze", [], [])
    monkeypatch.setattr(bibtex_import.metrics, "fetch_metrics",
                        fake_fetch)
    rec = {"doi": "10.1093/nar/25.24.4876",
           "title": "The CLUSTAL_X windows interface",
           "year": "1997",
           "authors": ["Julie D. Thompson", "Toby J. Gibson",
                       "F. Plewniak", "F. Jeanmougin"]}
    bibtex_import._enrich_with_openalex(rec)
    assert len(rec["authors"]) == 4          # not shrunk
    assert rec["citations"] == 39281         # enrichment still applied
    assert len(rec["authorships"]) == 1      # structured data kept as-is


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
