"""Tests for alexandria.author_image — author-photo storage and the
Wikidata portrait lookup. No network (HTTP injected), no display
(GdkPixbuf renders headless).

Runnable as `python3 -m tests.test_author_image` or via pytest.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

from alexandria import author_image


def _png_bytes(w, h):
    """A w×h solid PNG as bytes, via GdkPixbuf (no display needed)."""
    pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, w, h)
    pb.fill(0x336699ff)
    ok, data = pb.save_to_bufferv("png", [], [])
    assert ok
    return bytes(data)


AUTH = {"name": "Jane K", "openalex_id": "A123", "orcid": "0000-0001"}


def test_image_path_uses_key_precedence(tmp_path):
    p = author_image.image_path(AUTH, root=str(tmp_path))
    assert p == str(tmp_path / ".author-images" / "A123.png")
    q = author_image.image_path({"orcid": "0000-0001"}, root=str(tmp_path))
    assert q == str(tmp_path / ".author-images" / "0000-0001.png")


def test_image_path_none_without_identifier(tmp_path):
    assert author_image.image_path({"name": "X"}, root=str(tmp_path)) is None


def test_save_from_bytes_and_remove(tmp_path):
    p = author_image.save_image(AUTH, _png_bytes(64, 48), root=str(tmp_path))
    assert os.path.isfile(p)
    pb = GdkPixbuf.Pixbuf.new_from_file(p)
    assert (pb.get_width(), pb.get_height()) == (64, 48)
    assert author_image.remove_image(AUTH, root=str(tmp_path)) is True
    assert not os.path.isfile(p)
    assert author_image.remove_image(AUTH, root=str(tmp_path)) is False


def test_save_from_file_path(tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(_png_bytes(32, 32))
    p = author_image.save_image(AUTH, str(src), root=str(tmp_path))
    assert os.path.isfile(p)


def test_save_scales_long_side_to_512(tmp_path):
    p = author_image.save_image(AUTH, _png_bytes(1024, 256),
                                root=str(tmp_path))
    pb = GdkPixbuf.Pixbuf.new_from_file(p)
    assert pb.get_width() == 512
    assert pb.get_height() == 128   # aspect preserved


def test_save_without_identifier_raises(tmp_path):
    try:
        author_image.save_image({"name": "X"}, _png_bytes(8, 8),
                                root=str(tmp_path))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def _sparql_response(*img_urls):
    return {"results": {"bindings": [
        {"img": {"value": u}} for u in img_urls]}}


def test_wikidata_url_hit():
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return _sparql_response(
            "http://commons.wikimedia.org/wiki/Special:FilePath/Jane.jpg")

    u = author_image.wikidata_portrait_url("0000-0001", "A123", fake_get)
    assert u == ("http://commons.wikimedia.org/wiki/"
                 "Special:FilePath/Jane.jpg?width=512")
    # One SPARQL round-trip carrying both identifier properties + P18.
    assert len(calls) == 1
    q = calls[0]
    assert "P496" in q and "P10283" in q and "P18" in q
    assert "0000-0001" in q and "A123" in q


def test_wikidata_url_no_portrait():
    assert author_image.wikidata_portrait_url(
        "0000-0001", None, lambda u, headers, timeout:
        _sparql_response()) is None


def test_wikidata_url_http_failure():
    assert author_image.wikidata_portrait_url(
        "0000-0001", None, lambda u, headers, timeout: None) is None


def test_wikidata_url_no_identifiers():
    def boom(u, headers, timeout):
        raise AssertionError("must not be called")
    assert author_image.wikidata_portrait_url(None, None, boom) is None


def test_fetch_portrait_saves(tmp_path):
    def fake_json(url, headers, timeout):
        return _sparql_response(
            "http://commons.wikimedia.org/wiki/Special:FilePath/J.jpg")

    def fake_bytes(url, headers, timeout):
        assert url.endswith("?width=512")
        return _png_bytes(300, 400)

    p = author_image.fetch_wikidata_portrait(
        AUTH, root=str(tmp_path),
        http_get_json=fake_json, http_get_bytes=fake_bytes)
    assert p and os.path.isfile(p)


def test_fetch_portrait_none_when_missing(tmp_path):
    p = author_image.fetch_wikidata_portrait(
        AUTH, root=str(tmp_path),
        http_get_json=lambda u, headers, timeout: _sparql_response(),
        http_get_bytes=lambda u, headers, timeout: b"")
    assert p is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# --- Name fallback (only when identifiers find no item at all) ------

def _sparql_items(*pairs):
    """pairs of (item_uri, img_url) -> SPARQL bindings with both vars."""
    return {"results": {"bindings": [
        {"item": {"value": it}, "img": {"value": im}}
        for it, im in pairs]}}


def _fake_router(identifier_resp, name_resp):
    """Route identifier-lookup vs name-lookup queries by content."""
    def get(url, headers, timeout):
        if "P496" in url or "P10283" in url:
            return identifier_resp
        assert "P31" in url and "P18" in url
        return name_resp
    return get


def test_name_fallback_when_no_item(tmp_path):
    # Hoffbrand case: identifiers match nothing, but an exact-name
    # search finds exactly one human with a portrait.
    get = _fake_router(
        _sparql_response(),   # no item via identifiers
        _sparql_items(("http://www.wikidata.org/entity/Q1",
                       "http://commons.wikimedia.org/wiki/"
                       "Special:FilePath/AVH.jpg")))
    p = author_image.fetch_wikidata_portrait(
        dict(AUTH, name="Allan Victor Hoffbrand"), root=str(tmp_path),
        http_get_json=get,
        http_get_bytes=lambda u, headers, timeout: _png_bytes(64, 64))
    assert p and os.path.isfile(p)


def test_no_name_fallback_when_item_lacks_portrait(tmp_path):
    # Identifier search FOUND the author's item (no P18 on it). A
    # same-name impostor with a portrait must not be consulted.
    def get(url, headers, timeout):
        if "P496" in url or "P10283" in url:
            return {"results": {"bindings": [
                {"item": {"value": "http://www.wikidata.org/entity/Q9"}}]}}
        raise AssertionError("name fallback must not run")
    p = author_image.fetch_wikidata_portrait(
        dict(AUTH, name="Jane K"), root=str(tmp_path),
        http_get_json=get,
        http_get_bytes=lambda u, headers, timeout: _png_bytes(8, 8))
    assert p is None


def test_name_fallback_ambiguous_declines(tmp_path):
    get = _fake_router(
        _sparql_response(),
        _sparql_items(("http://www.wikidata.org/entity/Q1", "http://c/1.jpg"),
                      ("http://www.wikidata.org/entity/Q2", "http://c/2.jpg")))
    p = author_image.fetch_wikidata_portrait(
        dict(AUTH, name="John Smith"), root=str(tmp_path),
        http_get_json=get,
        http_get_bytes=lambda u, headers, timeout: _png_bytes(8, 8))
    assert p is None


def test_name_fallback_same_item_two_images_is_unique(tmp_path):
    get = _fake_router(
        _sparql_response(),
        _sparql_items(("http://www.wikidata.org/entity/Q1", "http://c/a.jpg"),
                      ("http://www.wikidata.org/entity/Q1", "http://c/b.jpg")))
    p = author_image.fetch_wikidata_portrait(
        dict(AUTH, name="Unique Person"), root=str(tmp_path),
        http_get_json=get,
        http_get_bytes=lambda u, headers, timeout: _png_bytes(8, 8))
    assert p and os.path.isfile(p)


def test_name_fallback_skipped_without_name(tmp_path):
    get = _fake_router(_sparql_response(), _sparql_items())
    p = author_image.fetch_wikidata_portrait(
        {"openalex_id": "A123"}, root=str(tmp_path),
        http_get_json=get,
        http_get_bytes=lambda u, headers, timeout: _png_bytes(8, 8))
    assert p is None


# --- download_image: og:image resolution for HTML drops -------------

def test_download_image_passes_through_image_bytes():
    png = _png_bytes(8, 8)
    out = author_image.download_image(
        "https://x/img.png",
        http_get_bytes=lambda u, headers, timeout: png)
    assert out == png


def test_download_image_resolves_og_image():
    # Dragging Wikipedia's infobox portrait delivers the File: page
    # (HTML), whose og:image points at the real file.
    png = _png_bytes(8, 8)
    html = (b'<!DOCTYPE html><html><head>'
            b'<meta property="og:image" '
            b'content="https://upload.wikimedia.org/real.jpg"/>'
            b'</head><body></body></html>')

    def get(url, headers, timeout):
        if url.endswith("File:X.jpg"):
            return html
        assert url == "https://upload.wikimedia.org/real.jpg"
        return png

    out = author_image.download_image(
        "https://en.wikipedia.org/wiki/File:X.jpg", http_get_bytes=get)
    assert out == png


def test_download_image_og_image_reversed_attrs_and_relative():
    png = _png_bytes(8, 8)
    html = (b'<html><head><meta content="/img/lead.jpg" '
            b'property="og:image"></head></html>')

    def get(url, headers, timeout):
        if url == "https://site.example/page":
            return html
        assert url == "https://site.example/img/lead.jpg"
        return png

    assert author_image.download_image(
        "https://site.example/page", http_get_bytes=get) == png


def test_download_image_twitter_image_fallback():
    png = _png_bytes(8, 8)
    html = (b'<html><head><meta name="twitter:image" '
            b'content="https://c/t.jpg"></head></html>')

    def get(url, headers, timeout):
        return html if url == "https://p/page" else png

    assert author_image.download_image(
        "https://p/page", http_get_bytes=get) == png


def test_download_image_html_without_image_meta_raises():
    html = b"<html><head><title>nope</title></head></html>"
    try:
        author_image.download_image(
            "https://p/page",
            http_get_bytes=lambda u, headers, timeout: html)
    except ValueError as e:
        assert "image" in str(e).lower()
    else:
        raise AssertionError("expected ValueError")
