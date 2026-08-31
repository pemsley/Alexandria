"""Supporting-information PDFs are supplements, not papers.

An SI file carries its parent article's DOI in its text, so
extraction handed it over and the SI record claimed to *be* that
paper: `ci0c01144_si_001.pdf` holds 10.1021/acs.jcim.0c01144 in this
library, and because the DOI was then taken, the real paper could
not import at all (observed 2026-08-30 with acs.jcim.4c02293).

So: recognise the supplement, record the parent in `si_of` rather
than in `doi`, and leave the DOI free for the paper itself.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import extract, importer, index, sidecar


# ---- detection ------------------------------------------------------

def test_publisher_si_filenames_are_recognised():
    """Real names from the library."""
    for name in ("13321_2020_429_MOESM2_ESM.pdf",
                 "41467_2025_67297_MOESM1_ESM.pdf",
                 "42004_2020_367_MOESM1_ESM.pdf",
                 "ci0c01144_si_001.pdf",
                 "jm1c01803_si_001.pdf",
                 "pnas.2524504123.sapp.pdf",
                 "ba5278sup1.pdf"):
        assert extract.is_supplementary(name), name


def test_ordinary_papers_are_not_supplementary():
    for name in ("s41467-025-56045-z.pdf", "sigmaa.pdf",
                 "acs.jcim.4c02293.pdf", "2006.11239v2.pdf",
                 "1-s2.0-S0022283695800379-molecular.pdf",
                 "supercomplex-assembly.pdf",     # 'sup' inside a word
                 "jumper2021highly.pdf"):
        assert not extract.is_supplementary(name), name


def test_an_extracted_title_can_confirm_it():
    """Springer SI files often extract as literally this."""
    assert extract.is_supplementary(
        "someting.pdf", title="Supplemental Information")
    assert extract.is_supplementary(
        "other.pdf", title="Supplementary Information")
    assert not extract.is_supplementary(
        "other.pdf", title="Supplementary motor area lesions")


# ---- the parent's DOI, from the filename where derivable ------------

def test_parent_doi_prefix_from_springer_bmc_names():
    assert extract.supplementary_parent_doi_prefix(
        "13321_2020_429_MOESM2_ESM.pdf") == "10.1186/s13321-020-00429"
    assert extract.supplementary_parent_doi_prefix(
        "41467_2025_67297_MOESM1_ESM.pdf") == "10.1038/s41467-025-67297"


def test_parent_doi_from_pnas_sapp():
    assert extract.supplementary_parent_doi_prefix(
        "pnas.2524504123.sapp.pdf") == "10.1073/pnas.2524504123"


def test_no_prefix_for_names_we_cannot_decode():
    assert extract.supplementary_parent_doi_prefix(
        "ba5278sup1.pdf") is None
    assert extract.supplementary_parent_doi_prefix(
        "s41467-025-56045-z.pdf") is None


# ---- the importer must not let a supplement claim the DOI -----------

def _quiet(monkeypatch, doi):
    monkeypatch.setattr(
        importer, "_build_record",
        lambda p: {"title": "Supporting Information", "authors": [],
                   "year": 2020, "journal": None, "doi": doi})
    monkeypatch.setattr(
        importer.thumbnail, "make_thumbnail", lambda *a, **k: None)
    monkeypatch.setattr(importer, "_schedule_pdb_indexing",
                        lambda *a: None)
    monkeypatch.setattr(
        importer.metrics, "is_preprint_doi", lambda d: False)


def _pdf(tmp_path, name):
    """Distinct bytes per file — identical content would trip the
    SHA-256 duplicate check before the DOI logic is reached."""
    p = str(tmp_path / name)
    with open(p, "wb") as fh:
        fh.write(b"%PDF fake " + name.encode())
    return p


def test_supplement_records_its_parent_and_claims_no_doi(
        tmp_path, monkeypatch):
    _quiet(monkeypatch, "10.1021/acs.jcim.0c01144")

    def explode(doi):
        raise AssertionError("enriched a supplement as if it were "
                             "the paper")

    monkeypatch.setattr(importer.metrics, "fetch_metrics", explode)
    monkeypatch.setattr(
        importer.jats, "fetch_and_store",
        lambda p, d: (_ for _ in ()).throw(
            AssertionError("fetched JATS for a supplement")))

    pdf = _pdf(tmp_path, "ci0c01144_si_001.pdf")
    conn = index.open_db(str(tmp_path / "lib.db"))
    rec, status = importer.import_pdf(conn, pdf)

    assert status == "new"
    assert rec["doi"] is None, "a supplement must not claim the DOI"
    assert rec["si_of"]["doi"] == "10.1021/acs.jcim.0c01144"


def test_the_real_paper_can_still_import_afterwards(
        tmp_path, monkeypatch):
    """The point of all this: the supplement no longer blocks it."""
    _quiet(monkeypatch, "10.1021/acs.jcim.0c01144")
    monkeypatch.setattr(importer.metrics, "fetch_metrics",
                        lambda d: (None,) * 12)
    monkeypatch.setattr(
        importer.jats, "fetch_and_store",
        lambda p, d: {"status": "no_pmcid", "pmcid": None,
                      "checked": "2026-08-31"})
    conn = index.open_db(str(tmp_path / "lib.db"))
    importer.import_pdf(conn, _pdf(tmp_path, "ci0c01144_si_001.pdf"))

    monkeypatch.setattr(
        importer, "_build_record",
        lambda p: {"title": "Understanding Ring Puckering",
                   "authors": ["A"], "year": 2021, "journal": "JCIM",
                   "doi": "10.1021/acs.jcim.0c01144"})
    rec, status = importer.import_pdf(
        conn, _pdf(tmp_path, "acs.jcim.0c01144.pdf"))
    assert status == "new", "the paper was blocked by its supplement"
    assert rec["doi"] == "10.1021/acs.jcim.0c01144"


def test_parent_title_is_filled_in_when_it_is_in_the_library(
        tmp_path, monkeypatch):
    conn = index.open_db(str(tmp_path / "lib.db"))
    parent = _pdf(tmp_path, "acs.jcim.0c01144.pdf")
    prec = sidecar.new_record(parent)
    prec.update({"title": "Understanding Ring Puckering",
                 "doi": "10.1021/acs.jcim.0c01144"})
    sc = sidecar.sidecar_path_for(parent)
    sidecar.write(sc, prec)
    index.upsert(conn, parent, sc, None, prec, os.path.getmtime(sc))

    _quiet(monkeypatch, "10.1021/acs.jcim.0c01144")
    monkeypatch.setattr(importer.metrics, "fetch_metrics",
                        lambda d: (None,) * 12)
    rec, _status = importer.import_pdf(
        conn, _pdf(tmp_path, "ci0c01144_si_001.pdf"))
    assert rec["si_of"]["title"] == "Understanding Ring Puckering"


def test_new_records_carry_the_field():
    assert "si_of" in sidecar.new_record("/x/y.pdf")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


def test_the_supplement_is_not_its_own_parent(tmp_path, monkeypatch):
    """A supplement being re-marked already has a row holding the
    parent's DOI (that is the bug being repaired), so the parent
    lookup must exclude the file itself — otherwise it copies its
    own wrongly-claimed title in as the parent's."""
    conn = index.open_db(str(tmp_path / "lib.db"))
    si = _pdf(tmp_path, "ci0c01144_si_001.pdf")
    rec = sidecar.new_record(si)
    rec.update({"title": "Wrongly claimed title",
                "doi": "10.1021/acs.jcim.0c01144"})
    sc = sidecar.sidecar_path_for(si)
    sidecar.write(sc, rec)
    index.upsert(conn, si, sc, None, rec, os.path.getmtime(sc))

    importer._mark_as_supplement(conn, rec, si)
    assert rec["si_of"]["title"] != "Wrongly claimed title"
    assert rec["si_of"]["title"] is None


# ---- a supplement inherits its parent's bibliographic identity -----

def test_supplement_takes_authors_and_year_from_its_parent(
        tmp_path, monkeypatch):
    """A supplement's authors *are* the paper's authors. Left to its
    own metadata, gm5104sup1.pdf offered ["staff"] — the account
    name of whoever ran the distiller on IUCr's production server.
    """
    conn = index.open_db(str(tmp_path / "lib.db"))
    parent = _pdf(tmp_path, "gm5104.pdf")
    prec = sidecar.new_record(parent)
    prec.update({"title": "High-confidence placement of fragments",
                 "authors": ["Shumeng Ma", "Matthew W. Bowler"],
                 "year": 2024,
                 "journal": "Acta Crystallographica Section D",
                 "doi": "10.1107/S2059798324004480"})
    sc = sidecar.sidecar_path_for(parent)
    sidecar.write(sc, prec)
    index.upsert(conn, parent, sc, None, prec, os.path.getmtime(sc))

    rec = {"title": "supplement", "authors": ["staff"], "year": None,
           "journal": None, "doi": "10.1107/S2059798324004480"}
    importer._mark_as_supplement(
        conn, rec, _pdf(tmp_path, "gm5104sup1.pdf"))

    assert rec["authors"] == ["Shumeng Ma", "Matthew W. Bowler"]
    assert rec["year"] == 2024
    assert rec["journal"] == "Acta Crystallographica Section D"
    assert rec["doi"] is None, "still must not claim the DOI"


def test_an_unknown_parent_leaves_the_supplement_alone(
        tmp_path, monkeypatch):
    conn = index.open_db(str(tmp_path / "lib.db"))
    rec = {"title": "supplement", "authors": ["staff"], "year": 2024,
           "journal": None, "doi": "10.1/absent"}
    importer._mark_as_supplement(
        conn, rec, _pdf(tmp_path, "x_si_001.pdf"))
    assert rec["authors"] == ["staff"]
    assert rec["si_of"]["doi"] == "10.1/absent"


def test_remarking_uses_the_parent_already_recorded(tmp_path):
    """Idempotence: a record already marked as a supplement has no
    `doi` of its own (that is the point), and its filename may carry
    no decodable identifier — gm5104sup1.pdf is IUCr's. The parent
    it already names is the thing to look it up by."""
    conn = index.open_db(str(tmp_path / "lib.db"))
    parent = _pdf(tmp_path, "gm5104.pdf")
    prec = sidecar.new_record(parent)
    prec.update({"title": "High-confidence placement",
                 "authors": ["Shumeng Ma"], "year": 2024,
                 "doi": "10.1107/S2059798324004480"})
    sc = sidecar.sidecar_path_for(parent)
    sidecar.write(sc, prec)
    index.upsert(conn, parent, sc, None, prec, os.path.getmtime(sc))

    rec = {"title": "supplement", "authors": ["staff"], "doi": None,
           "si_of": {"doi": "10.1107/S2059798324004480",
                     "title": "High-confidence placement"}}
    importer._mark_as_supplement(
        conn, rec, _pdf(tmp_path, "gm5104sup1.pdf"))
    assert rec["authors"] == ["Shumeng Ma"]
