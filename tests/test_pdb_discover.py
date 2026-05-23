"""Tests for metrics.fetch_pdb_publications' pure parser
(_parse_pdb_publications). Used by the "By PDB" Discover tab.

Runnable as `python3 -m tests.test_pdb_discover` (no pytest required)
or collectable by pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import metrics


# A trimmed PDBe publications response for 4HHB, with one synthetic
# secondary publication added: lacks a DOI, has a single author.
PAYLOAD = {
    "4hhb": [
        {
            "doi": "10.1016/0022-2836(84)90472-8",
            "title": "The crystal structure of human deoxyhaemoglobin "
                     "at 1.74 A resolution.",
            "pubmed_id": "6726807",
            "journal_info": {
                "pdb_abbreviation": "J. Mol. Biol.",
                "ISO_abbreviation": "J Mol Biol",
                "year": 1984,
            },
            "author_list": [
                {"full_name": "Fermi G"},
                {"full_name": "Perutz MF"},
                {"full_name": "Shaanan B"},
            ],
        },
        {
            # No DOI on this one — still parsed (skip happens in handler).
            "title": "Secondary methods note",
            "pubmed_id": "0000001",
            "journal_info": {
                "ISO_abbreviation": "Methods Enzymol",
                "year": 1985,
            },
            "author_list": [{"full_name": "Solo A"}],
        },
    ]
}


def test_parse_primary_publication():
    pubs = metrics._parse_pdb_publications(PAYLOAD, "4hhb")
    assert len(pubs) == 2
    p = pubs[0]
    assert p["doi"] == "10.1016/0022-2836(84)90472-8"
    assert p["title"].startswith("The crystal structure of human deoxy")
    assert p["pubmed_id"] == "6726807"
    assert p["year"] == 1984
    assert p["journal"] == "J Mol Biol"           # ISO_abbreviation wins
    assert p["authors"] == ["Fermi G", "Perutz MF", "Shaanan B"]
    assert p["first_author"] == "Fermi G"
    assert p["last_author"] == "Shaanan B"


def test_parse_keeps_entry_without_doi():
    pubs = metrics._parse_pdb_publications(PAYLOAD, "4hhb")
    p = pubs[1]
    assert p["doi"] is None
    assert p["title"] == "Secondary methods note"
    assert p["journal"] == "Methods Enzymol"
    assert p["authors"] == ["Solo A"]
    assert p["first_author"] == "Solo A"
    # Single-author paper: last_author is None per the spec (only
    # populated when there is more than one author).
    assert p["last_author"] is None


def test_parse_journal_fallback_to_pdb_abbreviation():
    payload = {"4hhb": [{
        "title": "x",
        "journal_info": {"pdb_abbreviation": "J Foo"},
        "author_list": [{"full_name": "A B"}],
    }]}
    pubs = metrics._parse_pdb_publications(payload, "4hhb")
    assert pubs[0]["journal"] == "J Foo"


def test_parse_missing_and_empty_inputs():
    assert metrics._parse_pdb_publications(None, "4hhb") == []
    assert metrics._parse_pdb_publications({}, "4hhb") == []
    assert metrics._parse_pdb_publications({"4hhb": []}, "4hhb") == []
    # Right shape but wrong key — also empty.
    assert metrics._parse_pdb_publications({"9zzz": [{"title": "t"}]},
                                            "4hhb") == []


# ---- Self-test runner ----

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
