from alexandria import bibtex_import


def test_br_from_metadata_full():
    resolved = {
        "first_author": "Kevin Cowtan",
        "authors": ["Kevin Cowtan", "Paul Emsley"],
        "title": "The Buccaneer software for automated model building",
        "year": 2006,
        "journal": "Acta Crystallographica D",
        "doi": "10.1107/s0907444906022116",
    }
    br = bibtex_import.br_from_metadata(resolved)
    assert br["bibtex_type"] == "article"
    assert br["title"] == resolved["title"]
    assert br["authors"] == resolved["authors"]
    assert br["year"] == "2006"
    assert br["journal"] == resolved["journal"]
    assert br["doi"] == resolved["doi"]
    assert br["bibtex_extra"] == {}
    # citekey: <surname><year><first-title-word>
    assert br["bibtex_key"] == "cowtan2006the"


def test_br_from_metadata_missing_fields_get_fallback_key():
    br = bibtex_import.br_from_metadata({})
    assert br["bibtex_key"].startswith("ref")
    assert br["year"] is None
    assert br["authors"] == []
