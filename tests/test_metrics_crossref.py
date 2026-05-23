"""Tests for the Crossref-authorships fallback in alexandria.metrics.

Runnable as `python3 -m tests.test_metrics_crossref` (no pytest
required) or collectable by pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


# A trimmed Crossref /works message: three authors, mixed ORCID
# presence, one multi-affiliation entry, modelled on science.adv3301.
MSG = {
    "is-referenced-by-count": 7,
    "title": ["Short RNA chaperones promote aggregation-resistant TDP-43"],
    "issued": {"date-parts": [[2026, 5, 7]]},
    "author": [
        {
            "given": "Katie E.",
            "family": "Copley",
            "ORCID": "https://orcid.org/0000-0003-0422-7475",
            "affiliation": [
                {"name": "Department of Biochemistry, UPenn"},
                {"name": "Neuroscience Graduate Group, UPenn"},
            ],
        },
        {
            "given": "Bede",
            "family": "Portz",
            "affiliation": [],
        },
        {
            "given": "James",
            "family": "Shorter",
            "ORCID": "https://orcid.org/0000-0001-5269-8533",
            "affiliation": [{"name": "Department of Biochemistry, UPenn"}],
        },
    ],
}


def test_builds_one_entry_per_named_author():
    auths = metrics._crossref_authorships(MSG)
    assert [a["name"] for a in auths] == [
        "Katie E. Copley", "Bede Portz", "James Shorter"]


def test_position_tags_first_middle_last():
    auths = metrics._crossref_authorships(MSG)
    assert [a["position"] for a in auths] == ["first", "middle", "last"]


def test_orcid_is_stripped_to_bare_id():
    auths = metrics._crossref_authorships(MSG)
    assert auths[0]["orcid"] == "0000-0003-0422-7475"
    assert auths[1]["orcid"] is None  # no ORCID supplied


def test_openalex_id_is_none_from_crossref():
    auths = metrics._crossref_authorships(MSG)
    assert all(a["openalex_id"] is None for a in auths)


def test_first_affiliation_used_as_institution():
    auths = metrics._crossref_authorships(MSG)
    assert auths[0]["institution"] == "Department of Biochemistry, UPenn"
    assert auths[1]["institution"] is None  # no affiliation


def test_author_without_name_is_skipped():
    msg = {"author": [
        {"given": "", "family": ""},
        {"given": "Jane", "family": "Doe"},
    ]}
    auths = metrics._crossref_authorships(msg)
    assert [a["name"] for a in auths] == ["Jane Doe"]
    assert auths[0]["position"] == "first"


def test_single_author_is_first():
    msg = {"author": [{"given": "Solo", "family": "Author"}]}
    auths = metrics._crossref_authorships(msg)
    assert auths[0]["position"] == "first"


def test_no_authors_returns_empty():
    assert metrics._crossref_authorships({}) == []
    assert metrics._crossref_authorships(None) == []


def test_fetch_metrics_falls_back_to_crossref_authorships(monkeypatch=None):
    # Manual monkeypatch (no pytest fixture dependency): force OpenAlex
    # to miss and Crossref to return our sample message.
    saved_oa = metrics._openalex_metrics
    saved_cr = metrics._fetch_crossref_work_message
    metrics._openalex_metrics = lambda doi: (
        None, [], None, [], [], None, None, None, None)
    metrics._fetch_crossref_work_message = lambda doi: MSG
    try:
        (n, src, kw, abstract, authorships, cby,
         oa_title, oa_year, is_oa, oa_status) = metrics.fetch_metrics(
            "10.1126/science.adv3301")
    finally:
        metrics._openalex_metrics = saved_oa
        metrics._fetch_crossref_work_message = saved_cr
    assert src == "crossref"
    assert n == 7
    assert [a["name"] for a in authorships] == [
        "Katie E. Copley", "Bede Portz", "James Shorter"]
    # oa_title carries the Crossref title so the importer's
    # cross-contamination guard has something to compare against.
    assert oa_title == MSG["title"][0]
    assert oa_year == 2026


# ---- Self-test runner (no pytest needed) ---------------------------


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        name = t.__name__
        try:
            t()
        except AssertionError as e:
            failures += 1
            print("FAIL  {}\n        {}".format(name, e))
        except Exception as e:
            failures += 1
            print("ERROR {}\n        {!r}".format(name, e))
        else:
            print("ok    {}".format(name))
    print()
    print("{} test(s), {} failure(s)".format(len(tests), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
