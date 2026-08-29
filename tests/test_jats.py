"""Tests for alexandria.jats — Europe PMC JATS full-text fetch and
store (v0: fetch-and-store beside the PDF, sidecar bookkeeping).

Network is monkeypatched throughout (`jats._get_json` /
`jats._get_bytes`); no HTTP happens here.
"""

import datetime
import os
import sys
import urllib.error

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import jats


def _epmc_search_payload(pmcid):
    entry = {"id": "12345", "doi": "10.1000/x"}
    if pmcid:
        entry["pmcid"] = pmcid
    return {"resultList": {"result": [entry]}}


def _http_404(url):
    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)


# ---- jats_path ----------------------------------------------------

def test_jats_path_appends_suffix():
    assert jats.jats_path("/lib/paper.pdf") == "/lib/paper.pdf.jats.xml"


# ---- pmcid_for_doi ------------------------------------------------

def test_pmcid_found(monkeypatch):
    monkeypatch.setattr(
        jats, "_get_json",
        lambda url: _epmc_search_payload("PMC7096066"))
    assert jats.pmcid_for_doi("10.1000/x") == "PMC7096066"


def test_pmcid_absent_from_result(monkeypatch):
    monkeypatch.setattr(
        jats, "_get_json", lambda url: _epmc_search_payload(None))
    assert jats.pmcid_for_doi("10.1000/x") is None


def test_pmcid_no_results(monkeypatch):
    monkeypatch.setattr(
        jats, "_get_json",
        lambda url: {"resultList": {"result": []}})
    assert jats.pmcid_for_doi("10.1000/x") is None


def test_pmcid_doi_is_quoted_into_query(monkeypatch):
    seen = {}

    def fake(url):
        seen["url"] = url
        return _epmc_search_payload("PMC1")

    monkeypatch.setattr(jats, "_get_json", fake)
    jats.pmcid_for_doi('10.1000/od(d)"chars')
    assert "od%28d%29%22chars" in seen["url"]


# ---- fetch_and_store ----------------------------------------------

def test_store_writes_file_and_reports(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        jats, "_get_json",
        lambda url: _epmc_search_payload("PMC7096066"))
    monkeypatch.setattr(
        jats, "_get_bytes", lambda url: b"<article>hi</article>")

    block = jats.fetch_and_store(pdf, "10.1000/x")

    assert block["status"] == "stored"
    assert block["pmcid"] == "PMC7096066"
    assert block["checked"] == datetime.date.today().isoformat()
    with open(jats.jats_path(pdf), "rb") as fh:
        assert fh.read() == b"<article>hi</article>"


def test_store_leaves_no_tmp_litter(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        jats, "_get_json", lambda url: _epmc_search_payload("PMC1"))
    monkeypatch.setattr(jats, "_get_bytes", lambda url: b"<a/>")
    jats.fetch_and_store(pdf, "10.1000/x")
    names = sorted(os.listdir(tmp_path))
    assert names == ["paper.pdf.jats.xml"]


def test_no_pmcid(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        jats, "_get_json", lambda url: _epmc_search_payload(None))
    block = jats.fetch_and_store(pdf, "10.1000/x")
    assert block["status"] == "no_pmcid"
    assert not os.path.exists(jats.jats_path(pdf))


def test_no_fulltext_on_404(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")
    monkeypatch.setattr(
        jats, "_get_json", lambda url: _epmc_search_payload("PMC1"))
    monkeypatch.setattr(jats, "_get_bytes", _http_404)
    block = jats.fetch_and_store(pdf, "10.1000/x")
    assert block["status"] == "no_fulltext"
    assert block["pmcid"] == "PMC1"
    assert not os.path.exists(jats.jats_path(pdf))


def test_transport_error_reported_as_error(tmp_path, monkeypatch):
    pdf = str(tmp_path / "paper.pdf")

    def boom(url):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(jats, "_get_json", boom)
    block = jats.fetch_and_store(pdf, "10.1000/x")
    assert block["status"] == "error"


# ---- fulltext_siblings (card chips) --------------------------------

def test_fulltext_siblings_empty_when_nothing_stored(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    assert jats.fulltext_siblings(pdf) == []


def test_fulltext_siblings_reports_jats(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    with open(jats.jats_path(pdf), "wb") as fh:
        fh.write(b"<article/>")
    got = jats.fulltext_siblings(pdf)
    assert len(got) == 1
    label, tip = got[0]
    assert label == "JATS"
    assert "JATS" in tip


def test_fulltext_siblings_none_path():
    assert jats.fulltext_siblings(None) == []


# ---- should_attempt (backfill skip logic) --------------------------

def _days_ago(n):
    return (datetime.date.today()
            - datetime.timedelta(days=n)).isoformat()


def test_attempt_when_never_tried(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    assert jats.should_attempt({}, pdf) is True


def test_skip_when_file_on_disk(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    with open(jats.jats_path(pdf), "wb") as fh:
        fh.write(b"<a/>")
    assert jats.should_attempt({}, pdf) is False


def test_skip_recent_negative_check(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    rec = {"jats": {"status": "no_fulltext", "checked": _days_ago(2)}}
    assert jats.should_attempt(rec, pdf) is False


def test_retry_stale_negative_check(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    rec = {"jats": {"status": "no_fulltext", "checked": _days_ago(40)}}
    assert jats.should_attempt(rec, pdf) is True


def test_retry_after_error_regardless_of_age(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    rec = {"jats": {"status": "error", "checked": _days_ago(0)}}
    assert jats.should_attempt(rec, pdf) is True


def test_stored_but_file_missing_retries(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    rec = {"jats": {"status": "stored", "checked": _days_ago(2)}}
    assert jats.should_attempt(rec, pdf) is True


def test_garbage_checked_date_is_treated_as_stale(tmp_path):
    pdf = str(tmp_path / "paper.pdf")
    rec = {"jats": {"status": "no_fulltext", "checked": "not-a-date"}}
    assert jats.should_attempt(rec, pdf) is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
