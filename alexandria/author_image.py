"""Author photos: storage under the library root and the Wikidata
portrait lookup.

Images live at `<library_root>/.author-images/<key>.png`, where
`<key>` is `index.author_trail_key` (OpenAlex ID, else ORCID) — the
same identity the Authors-window trail uses, so a photo saved from
any entry point shows everywhere. Everything is normalized to a
≤512 px PNG on save, making the path deterministic (no DB column).

The Wikidata lookup deliberately has its own tiny HTTP helpers
instead of metrics._http_get_json: Wikidata must never interact with
the OpenAlex circuit breaker, and injecting the getters keeps the
logic testable without network.
"""

import json
import os
import tempfile
import urllib.parse
import urllib.request

import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio

from . import index
from . import prefs

IMAGE_DIR_NAME = ".author-images"
MAX_SIDE = 512
_WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
_UA = "alexandria/0.2 (mailto:alexandria@example.org) author-photo"


def _images_dir(root=None):
    return os.path.join(root or prefs.get_library_root(),
                        IMAGE_DIR_NAME)


def image_path(authorship, root=None):
    """Deterministic photo path for this author, or None when the
    authorship has neither OpenAlex ID nor ORCID (same gate as the
    trail — no identity, nowhere stable to keep a photo)."""
    key = index.author_trail_key(authorship)
    if not key:
        return None
    return os.path.join(_images_dir(root), key + ".png")


def save_image(authorship, source, root=None):
    """Normalize `source` (a filesystem path or raw bytes) to a
    ≤512 px PNG at image_path. Atomic write (tmp + rename) so a
    concurrent reader never sees a half-written file. Returns the
    path. Raises ValueError without an identifier; GLib.Error when
    the data isn't a decodable image — callers surface the message."""
    path = image_path(authorship, root)
    if path is None:
        raise ValueError("author has no OpenAlex ID or ORCID")
    if isinstance(source, (bytes, bytearray)):
        stream = Gio.MemoryInputStream.new_from_data(bytes(source))
        pb = GdkPixbuf.Pixbuf.new_from_stream(stream)
    else:
        pb = GdkPixbuf.Pixbuf.new_from_file(source)
    w, h = pb.get_width(), pb.get_height()
    long_side = max(w, h)
    if long_side > MAX_SIDE:
        scale = MAX_SIDE / float(long_side)
        pb = pb.scale_simple(max(1, int(w * scale)),
                             max(1, int(h * scale)),
                             GdkPixbuf.InterpType.BILINEAR)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".png",
                               dir=os.path.dirname(path))
    os.close(fd)
    try:
        pb.savev(tmp, "png", [], [])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def remove_image(authorship, root=None):
    """Delete the stored photo. True if a file was removed."""
    path = image_path(authorship, root)
    if path and os.path.isfile(path):
        os.unlink(path)
        return True
    return False


def _http_get_json(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get_bytes(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _run_sparql(query, http_get_json):
    """Execute a SPARQL query, return the bindings list ([] on any
    empty/failed response)."""
    url = _WIKIDATA_SPARQL + "?" + urllib.parse.urlencode(
        [("query", query), ("format", "json")])
    data = http_get_json(
        url, {"User-Agent": _UA, "Accept": "application/sparql-results+json"},
        20)
    if not data:
        return []
    return (data.get("results") or {}).get("bindings") or []


def _sized(img_url):
    # SPARQL returns a Special:FilePath URL; a width parameter makes
    # Commons serve a reasonable thumbnail instead of the original
    # (which can be tens of MB).
    return img_url + "?width={}".format(MAX_SIDE)


def _identifier_lookup(orcid, openalex_id, http_get_json):
    """(item_found, image_url|None) for the identifier search. The
    distinction matters: an item that exists but has no P18 must NOT
    fall through to the name search — a same-name impostor with a
    portrait would win it."""
    clauses = []
    if orcid:
        clauses.append('{{ ?item wdt:P496 "{}" }}'.format(orcid))
    if openalex_id:
        clauses.append('{{ ?item wdt:P10283 "{}" }}'.format(openalex_id))
    if not clauses:
        return (False, None)
    query = ("SELECT ?item ?img WHERE {{ {} "
             "OPTIONAL {{ ?item wdt:P18 ?img }} }} LIMIT 1"
             .format(" UNION ".join(clauses) + " . "))
    bindings = _run_sparql(query, http_get_json)
    if not bindings:
        return (False, None)
    img = (bindings[0].get("img") or {}).get("value")
    return (True, _sized(img) if img else None)


def wikidata_portrait_url(orcid, openalex_id, http_get_json):
    """Commons image URL (sized to 512 px) for the researcher's
    Wikidata P18 portrait, or None when there is no item or no
    portrait. One SPARQL query carries both identifier properties —
    P496 (ORCID) and P10283 (OpenAlex author ID) — as a UNION, so
    whichever the author has can match."""
    return _identifier_lookup(orcid, openalex_id, http_get_json)[1]


def wikidata_portrait_url_by_name(name, http_get_json):
    """Last-resort lookup for authors whose Wikidata item carries
    neither identifier (common for pre-ORCID-era figures): exact
    English label/alias match, restricted to humans (P31 Q5) that
    have a portrait. Only an unambiguous hit — exactly one distinct
    item — is trusted; anything else returns None, because the
    failure mode of guessing is a confident wrong face."""
    if not name:
        return None
    safe = name.replace('"', "")
    query = (
        'SELECT ?item ?img WHERE {{ '
        '?item wdt:P31 wd:Q5 ; wdt:P18 ?img . '
        '{{ ?item rdfs:label "{0}"@en }} UNION '
        '{{ ?item skos:altLabel "{0}"@en }} }} LIMIT 5'.format(safe))
    bindings = _run_sparql(query, http_get_json)
    items = {}
    for b in bindings:
        item = (b.get("item") or {}).get("value")
        img = (b.get("img") or {}).get("value")
        if item and img:
            items.setdefault(item, img)
    if len(items) != 1:
        return None
    return _sized(next(iter(items.values())))


def fetch_wikidata_portrait(authorship, root=None,
                            http_get_json=None, http_get_bytes=None):
    """Look up and store the author's Wikidata portrait. Returns the
    saved path, or None when Wikidata has no item / no portrait.
    Network errors propagate — interactive callers catch and show
    the message.

    Identifier search first (ORCID / OpenAlex ID). Only when that
    finds NO item at all does the exact-name fallback run — an item
    that exists without a portrait is a definitive answer, and
    letting a same-name item override it would hang the wrong face
    on the author."""
    get_json = http_get_json or _http_get_json
    get_bytes = http_get_bytes or _http_get_bytes
    item_found, url = _identifier_lookup(
        authorship.get("orcid"), authorship.get("openalex_id"), get_json)
    if url is None and not item_found:
        url = wikidata_portrait_url_by_name(
            authorship.get("name"), get_json)
    if not url:
        return None
    data = get_bytes(url, {"User-Agent": _UA}, 30)
    return save_image(authorship, data, root=root)
