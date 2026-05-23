"""Tests for alexandria.bibtex parse/write.

Runnable as `python3 -m tests.test_bibtex` (no pytest required) or
collectable by pytest. Each test is a top-level `test_*` function.
"""

import os
import sys

# Allow running as `python3 -m tests.test_bibtex` from the project root
# without an editable install.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import bibtex, bibtex_export


# ---- Fixtures ------------------------------------------------------

SIMPLE = r"""
@article{smith2024foo,
  author  = {Smith, Jane and Doe, John},
  title   = {A Pithy Title with ATP and Co-factors},
  journal = {Journal of Things},
  year    = {2024},
  volume  = {5},
  number  = {2},
  pages   = {123--130},
  doi     = {10.1234/abc.5678}
}
"""

WITH_BRACES = r"""
@article{nested2023,
  author = {Crick, F. and Watson, J.},
  title  = {Structure of {DNA} and {ATP}},
  year   = {1953},
  doi    = {10.1038/171737a0}
}
"""

WITH_FILE = r"""
@article{withfile2020,
  author = {Tester, T.},
  title  = {With a File Field},
  year   = {2020},
  file   = {:/home/paule/pdfs/foo.pdf:pdf}
}
"""

MIXED_LIBRARY = r"""
@article{first2024,
  author  = {Adams, Ann},
  title   = {First entry},
  journal = {Journal A},
  year    = {2024},
  doi     = {10.1/a}
}
@misc{second2024,
  author = {Brown, Bob},
  title  = {Second entry, no journal},
  year   = {2024}
}
@inproceedings{third2025,
  author    = {Cross, Carol},
  title     = {In a conference},
  booktitle = {Proceedings of Big Conf},
  year      = {2025},
  pages     = {1--10}
}
"""


# ---- Helpers -------------------------------------------------------


def _diff_records(a, b):
    """Return a list of human-readable differences between two record
    lists. Empty list = identical (modulo dict-key order)."""
    out = []
    if len(a) != len(b):
        out.append("len mismatch: {} vs {}".format(len(a), len(b)))
        return out
    for i, (ra, rb) in enumerate(zip(a, b)):
        keys = set(ra) | set(rb)
        for k in sorted(keys):
            va, vb = ra.get(k), rb.get(k)
            if va != vb:
                out.append(
                    "entry[{}].{}: {!r} != {!r}".format(i, k, va, vb))
    return out


# ---- Tests ---------------------------------------------------------


def test_parse_simple_fields():
    recs = bibtex.parse(SIMPLE)
    assert len(recs) == 1
    r = recs[0]
    assert r["bibtex_key"] == "smith2024foo"
    assert r["bibtex_type"] == "article"
    assert r["title"] == "A Pithy Title with ATP and Co-factors"
    assert r["authors"] == ["Jane Smith", "John Doe"]
    assert r["year"] == 2024
    assert r["journal"] == "Journal of Things"
    assert r["doi"] == "10.1234/abc.5678"
    assert r["file"] is None
    # Non-promoted fields land in bibtex_extra. The LaTeX middleware
    # turns BibTeX `--` into a Unicode en-dash, which is the correct
    # rendering — so we expect "123–130" here, not "123--130".
    assert r["bibtex_extra"] == {
        "volume": "5",
        "number": "2",
        "pages": "123–130",
    }


def test_parse_strips_inner_braces():
    recs = bibtex.parse(WITH_BRACES)
    r = recs[0]
    assert r["title"] == "Structure of DNA and ATP"
    assert r["authors"] == ["F. Crick", "J. Watson"]


def test_parse_file_field():
    recs = bibtex.parse(WITH_FILE)
    r = recs[0]
    assert r["file"] == "/home/paule/pdfs/foo.pdf"


def test_parse_inproceedings_falls_back_to_booktitle():
    recs = bibtex.parse(MIXED_LIBRARY)
    third = next(r for r in recs if r["bibtex_key"] == "third2025")
    assert third["bibtex_type"] == "inproceedings"
    # No `journal=` but `booktitle=` is present, so it ends up there.
    assert third["journal"] == "Proceedings of Big Conf"
    # `1--10` is decoded to en-dash by the LaTeX middleware.
    assert third["bibtex_extra"] == {"pages": "1–10"}


def test_write_produces_parseable_output():
    recs = bibtex.parse(SIMPLE)
    out = bibtex.write(recs)
    assert "@article" in out
    assert "smith2024foo" in out
    # Round-trip: parsing the output gives back the same records.
    recs2 = bibtex.parse(out)
    diffs = _diff_records(recs, recs2)
    assert not diffs, "round-trip differences:\n  " + "\n  ".join(diffs)


def test_round_trip_mixed_library():
    recs1 = bibtex.parse(MIXED_LIBRARY)
    out = bibtex.write(recs1)
    recs2 = bibtex.parse(out)
    diffs = _diff_records(recs1, recs2)
    assert not diffs, "round-trip differences:\n  " + "\n  ".join(diffs)


def test_round_trip_with_file():
    recs1 = bibtex.parse(WITH_FILE)
    out = bibtex.write(recs1)
    recs2 = bibtex.parse(out)
    diffs = _diff_records(recs1, recs2)
    assert not diffs, "round-trip differences:\n  " + "\n  ".join(diffs)


def test_real_world_bibfile_parses_completely():
    """The user's `all-my-citations.bib` should parse all entries —
    including those rejected as `DuplicateFieldKeyBlock` (e.g. an
    entry with two `url = {...}` lines)."""
    path = os.path.join(ROOT, "all-my-citations.bib")
    if not os.path.isfile(path):
        return  # repo-local test file; skip silently if absent
    recs = bibtex.parse(path)
    # Source has 37 `^@` lines; we expect 37 records back.
    with open(path) as f:
        n_in_source = sum(1 for line in f if line.startswith("@"))
    assert len(recs) == n_in_source, (
        "parsed {} but the file has {} `@`-led blocks".format(
            len(recs), n_in_source))


# Promoted fields that we *guarantee* round-trip; abstract / keywords
# / note may contain math-mode LaTeX that the decoder normalises
# differently on the second pass.
_STRICT_ROUND_TRIP_KEYS = (
    "bibtex_key", "bibtex_type", "title", "authors", "year",
    "journal", "doi", "file",
)


def test_real_world_bibfile_round_trip_promoted_fields():
    """For every entry in the real file, the promoted fields survive
    parse → write → parse exactly. `bibtex_extra` is best-effort and
    not checked here — math-mode LaTeX in abstracts can re-interpret
    on the second pass."""
    path = os.path.join(ROOT, "all-my-citations.bib")
    if not os.path.isfile(path):
        return
    recs1 = bibtex.parse(path)
    out = bibtex.write(recs1)
    recs2 = bibtex.parse(out)
    assert len(recs1) == len(recs2)
    diffs = []
    for i, (a, b) in enumerate(zip(recs1, recs2)):
        for k in _STRICT_ROUND_TRIP_KEYS:
            if a.get(k) != b.get(k):
                diffs.append("entry[{}] {}={!r}  vs  {!r}".format(
                    i, k, a.get(k), b.get(k)))
    assert not diffs, "promoted-field drift:\n  " + "\n  ".join(diffs)


def test_field_order_is_irrelevant_to_round_trip():
    """Two BibTeX texts with the same fields in different orders
    should parse to identical record dicts."""
    a = r"""@article{x,
        author = {Z, Z}, title = {T}, year = {2020}, doi = {10.1/x}
    }"""
    b = r"""@article{x,
        doi = {10.1/x}, year = {2020}, title = {T}, author = {Z, Z}
    }"""
    ra = bibtex.parse(a)
    rb = bibtex.parse(b)
    diffs = _diff_records(ra, rb)
    assert not diffs, diffs


# ---- Export tests --------------------------------------------------


def test_export_preserves_promoted_fields_through_round_trip():
    """A sidecar dict → bibtex record → BibTeX text → parsed record
    should preserve all the promoted fields exactly."""
    sc = {
        "title": "A Pithy Title",
        "authors": ["Jane Smith", "John Doe"],
        "year": 2024,
        "journal": "Journal of Things",
        "doi": "10.1234/abc",
        "bibtex_key": "smith2024pithy",
        "bibtex_type": "article",
        "bibtex_extra": {"volume": "5", "pages": "1–10"},
    }
    br = bibtex_export.sidecar_to_bibtex_record(sc, "/tmp/x.pdf")
    text = bibtex.write([br])
    parsed = bibtex.parse(text)
    assert len(parsed) == 1
    p = parsed[0]
    assert p["bibtex_key"] == "smith2024pithy"
    assert p["bibtex_type"] == "article"
    assert p["title"] == "A Pithy Title"
    assert p["authors"] == ["Jane Smith", "John Doe"]
    assert p["year"] == 2024
    assert p["journal"] == "Journal of Things"
    assert p["doi"] == "10.1234/abc"
    assert p["file"] == "/tmp/x.pdf"
    assert p["bibtex_extra"] == {"volume": "5", "pages": "1–10"}


def test_export_autogenerates_key_when_absent():
    sc = {
        "title": "Features and development of Coot",
        "authors": ["P. Emsley", "B. Lohkamp", "W. G. Scott", "K. Cowtan"],
        "year": 2010,
        "journal": "Acta Crystallographica D",
        "doi": "10.1107/S0907444910007493",
    }
    br = bibtex_export.sidecar_to_bibtex_record(sc, "/some/coot.pdf")
    # surname + year + first significant title word.
    assert br["bibtex_key"] == "emsley2010features"
    assert br["bibtex_type"] == "article"   # has journal


def test_export_default_type_misc_when_no_journal():
    sc = {"title": "Standalone note", "authors": ["X. Y."], "year": 2023}
    br = bibtex_export.sidecar_to_bibtex_record(sc)
    assert br["bibtex_type"] == "misc"


def test_export_skips_file_field_for_ghost_paths():
    sc = {"title": "Just metadata", "authors": ["X. Y."], "year": 2023,
          "bibtex_key": "y2023just"}
    # A `bibtex:<key>` synthetic path means "no PDF on disk".
    br = bibtex_export.sidecar_to_bibtex_record(sc, "bibtex:y2023just")
    assert br["file"] is None


def test_export_dedupes_repeated_keys():
    """Two records with the same bibtex_key — second gets `_2` suffix."""
    a = bibtex_export.sidecar_to_bibtex_record(
        {"title": "First", "authors": ["A. Z."], "year": 2020,
         "bibtex_key": "z2020"})
    b = bibtex_export.sidecar_to_bibtex_record(
        {"title": "Second", "authors": ["A. Z."], "year": 2020,
         "bibtex_key": "z2020"})
    text = bibtex_export.records_to_text([a, b])
    parsed = bibtex.parse(text)
    keys = [p["bibtex_key"] for p in parsed]
    assert keys == ["z2020", "z2020_2"]


def test_export_round_trip_via_parse_then_export():
    """Parse the SIMPLE fixture, export it back as BibTeX, re-parse —
    everything should match."""
    recs = bibtex.parse(SIMPLE)
    # Round-trip: synthesise sidecar-style records (since SIMPLE
    # already has all the fields, the bibtex.parse output IS in the
    # right shape — promoted fields plus bibtex_extra).
    text = bibtex_export.records_to_text(recs)
    re_parsed = bibtex.parse(text)
    diffs = _diff_records(recs, re_parsed)
    assert not diffs, "diffs after export round-trip:\n  " + "\n  ".join(diffs)


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
