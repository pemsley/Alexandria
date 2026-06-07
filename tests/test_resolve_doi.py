from alexandria import metrics


def test_resolve_doi_prefers_openalex(monkeypatch):
    sentinel = {"doi": "10.1/x", "title": "From OpenAlex",
                "authors": ["A B"], "year": 2020, "journal": "J",
                "first_author": "A B", "last_author": "A B",
                "is_oa": True, "oa_url": "http://x/p.pdf"}
    monkeypatch.setattr(metrics, "fetch_work_by_doi", lambda d: sentinel)
    monkeypatch.setattr(metrics, "_fetch_crossref_work_message",
                        lambda d: (_ for _ in ()).throw(
                            AssertionError("CrossRef must not be called")))
    assert metrics.resolve_doi("10.1/x") is sentinel


def test_resolve_doi_falls_back_to_crossref(monkeypatch):
    monkeypatch.setattr(metrics, "fetch_work_by_doi", lambda d: None)
    msg = {
        "title": ["From CrossRef"],
        "author": [{"given": "Jane", "family": "Roe"},
                   {"given": "John", "family": "Doe"}],
        "container-title": ["Acta Cryst D"],
        "issued": {"date-parts": [[2021, 5]]},
        "DOI": "10.1/y",
    }
    monkeypatch.setattr(metrics, "_fetch_crossref_work_message",
                        lambda d: msg)
    out = metrics.resolve_doi("10.1/y")
    assert out["title"] == "From CrossRef"
    assert out["authors"] == ["Jane Roe", "John Doe"]
    assert out["first_author"] == "Jane Roe"
    assert out["last_author"] == "John Doe"
    assert out["journal"] == "Acta Cryst D"
    assert out["year"] == 2021
    assert out["doi"] == "10.1/y"
    assert out["is_oa"] is False
    assert out["oa_url"] is None


def test_resolve_doi_none_when_both_miss(monkeypatch):
    monkeypatch.setattr(metrics, "fetch_work_by_doi", lambda d: None)
    monkeypatch.setattr(metrics, "_fetch_crossref_work_message",
                        lambda d: None)
    assert metrics.resolve_doi("10.1/z") is None


def test_resolve_doi_empty():
    assert metrics.resolve_doi("") is None
    assert metrics.resolve_doi(None) is None
