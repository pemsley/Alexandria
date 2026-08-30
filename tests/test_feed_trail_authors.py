"""Flag feed articles written by authors the user has looked up
before (the `author_trail`).

Matching is on identifiers only — OpenAlex ID or ORCID. Names are
not a fallback: the trail holds a 'Yulin Zhang', and the Acta F feed
is full of other Zhangs.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import feed, index

PEARCE = {"name": "Nicholas M. Pearce",
          "orcid": "0000-0002-6693-8603",
          "openalex_id": "A5044544379"}
AGIRRE = {"name": "Jon Agirre", "orcid": "0000-0002-1086-0253",
          "openalex_id": "A5030493753"}
TRAIL = [PEARCE, AGIRRE]


def _authorship(name, orcid=None, openalex_id=None):
    return {"name": name, "orcid": orcid,
            "openalex_id": openalex_id}


# ---- matcher -------------------------------------------------------

def test_matches_on_openalex_id():
    arts = [_authorship("N. M. Pearce", openalex_id="A5044544379")]
    got = feed.match_trail_authors(arts, TRAIL)
    assert [e["name"] for e in got] == ["Nicholas M. Pearce"]


def test_matches_on_orcid_including_url_form():
    arts = [_authorship("J. Agirre",
                        orcid="https://orcid.org/0000-0002-1086-0253")]
    got = feed.match_trail_authors(arts, TRAIL)
    assert [e["name"] for e in got] == ["Jon Agirre"]


def test_same_surname_different_person_is_not_matched():
    """The whole reason identifiers are the only key."""
    arts = [_authorship("Bing Zhang", openalex_id="A9999999999"),
            _authorship("Nicholas M. Pearce")]      # no identifiers
    assert feed.match_trail_authors(arts, TRAIL) == []


def test_no_name_fallback_even_for_an_exact_name():
    arts = [_authorship("Jon Agirre")]
    assert feed.match_trail_authors(arts, TRAIL) == []


def test_each_trail_author_reported_once():
    arts = [_authorship("J. Agirre", openalex_id="A5030493753"),
            _authorship("Jon Agirre",
                        orcid="0000-0002-1086-0253")]
    assert len(feed.match_trail_authors(arts, TRAIL)) == 1


def test_empty_inputs():
    assert feed.match_trail_authors([], TRAIL) == []
    assert feed.match_trail_authors(
        [_authorship("X", openalex_id="A1")], []) == []
    assert feed.match_trail_authors(None, None) == []


# ---- the identifiers must survive fetch and storage -----------------

def test_openalex_normaliser_keeps_identifiers():
    work = {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1/x",
        "title": "T",
        "authorships": [{
            "author": {
                "display_name": "Nicholas M. Pearce",
                "id": "https://openalex.org/A5044544379",
                "orcid": "https://orcid.org/0000-0002-6693-8603"}}],
    }
    out = feed._normalize_openalex_item(work)
    assert out["authors"] == ["Nicholas M. Pearce"]
    a = out["authorships"][0]
    assert a["openalex_id"] == "A5044544379"
    assert a["orcid"] == "0000-0002-6693-8603"


def test_crossref_normaliser_keeps_orcid():
    item = {
        "DOI": "10.1/x",
        "title": ["T"],
        "author": [{"given": "Jon", "family": "Agirre",
                    "ORCID": "http://orcid.org/0000-0002-1086-0253"},
                   {"given": "No", "family": "Orcid"}],
    }
    out = feed._normalize_crossref_item(item)
    assert out["authors"] == ["Jon Agirre", "No Orcid"]
    assert out["authorships"][0]["orcid"] == "0000-0002-1086-0253"
    assert out["authorships"][1]["orcid"] is None


def test_authorships_round_trip_through_the_database(tmp_path):
    conn = index.open_db(str(tmp_path / "lib.db"))
    sub_id = index.add_subscription(
        conn, "journal_issn", "Acta F", "1234-5678")
    index.upsert_discovered(conn, sub_id, {
        "doi": "10.1/x", "title": "T", "authors": ["Jon Agirre"],
        "authorships": [_authorship("Jon Agirre",
                                    orcid="0000-0002-1086-0253",
                                    openalex_id="A5030493753")],
    })
    row = index.discovered_for(conn, sub_id)[0]
    stored = json.loads(row["authorships_json"])
    assert stored[0]["openalex_id"] == "A5030493753"
    assert feed.match_trail_authors(stored, TRAIL)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
