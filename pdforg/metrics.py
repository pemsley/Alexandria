"""Citation-count lookup. OpenAlex first, CrossRef fallback.

Network calls are timeout-bounded and never raise — they return (None, None)
on any failure so the caller can decide whether to retry later.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from .identity import maintainer_email

OPENALEX_MAILTO = maintainer_email()
OPENALEX_UA = os.environ.get(
    "PDFORG_OPENALEX_UA",
    "pdforg/0.1 (mailto:{})".format(OPENALEX_MAILTO))
CROSSREF_UA = os.environ.get(
    "PDFORG_CROSSREF_UA",
    "pdforg/0.1 (mailto:{})".format(OPENALEX_MAILTO))


def today_iso():
    return date.today().isoformat()


# Retry budget for transient HTTP failures. OpenAlex's free polite pool
# returns 429 when you exceed 10 req/sec, which is easy to hit during
# bulk import; CrossRef can be flaky too. We retry on 429 (honouring
# Retry-After) and 5xx, with a hard cap so a flapping endpoint can't
# stall the import indefinitely.
_HTTP_MAX_RETRIES = 3
_HTTP_RETRY_AFTER_CAP_SECONDS = 30.0
_HTTP_BACKOFF_BASE = 1.0


def _http_get_json(url, headers, timeout):
    """GET `url`, return parsed JSON dict, or None on persistent failure.

    Retries on HTTP 429 (Retry-After honoured, capped) and 5xx
    (exponential backoff). All other errors return None immediately."""
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            retry_ok = e.code == 429 or 500 <= e.code < 600
            if not retry_ok or attempt >= _HTTP_MAX_RETRIES:
                return None
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(ra) if ra is not None else _HTTP_BACKOFF_BASE
                except (TypeError, ValueError):
                    delay = _HTTP_BACKOFF_BASE
                delay = min(max(delay, 0.5), _HTTP_RETRY_AFTER_CAP_SECONDS)
            else:
                delay = _HTTP_BACKOFF_BASE * (2 ** attempt)
            time.sleep(delay)
            attempt += 1
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt >= _HTTP_MAX_RETRIES:
                return None
            time.sleep(_HTTP_BACKOFF_BASE * (2 ** attempt))
            attempt += 1
        except Exception:
            return None


# Drop OpenAlex concepts below this confidence score. 0.4 keeps the
# specific topics (e.g. "Glycoprotein") and trims the generic ones
# ("Chemistry", "Biology") that OpenAlex sprays on most papers.
KEYWORD_SCORE_THRESHOLD = 0.4
KEYWORD_LIMIT = 8


def fetch_metrics(doi):
    """Return (count, source, keywords, abstract, authorships,
    citations_by_year).

    citations_by_year is a list of {year, count} dicts, oldest-first
    (capped by OpenAlex at ~10 years). Any field may be None / []
    depending on what OpenAlex / CrossRef returned."""
    if not doi:
        return None, None, [], None, [], []
    n, kw, abstract, authorships, cby = _openalex_metrics(doi)
    if n is not None:
        return n, "openalex", kw, abstract, authorships, cby
    n = _crossref_count(doi)
    if n is not None:
        return n, "crossref", [], None, [], []
    return None, None, [], None, [], []


def fetch_citation_count(doi):
    """Backward-compatible wrapper returning just (count, source)."""
    n, src, _, _, _, _ = fetch_metrics(doi)
    return n, src


def _strip_url_prefix(s, prefixes):
    if not s:
        return None
    s = s.strip()
    low = s.lower()
    for p in prefixes:
        if low.startswith(p):
            return s[len(p):]
    return s or None


def _strip_orcid(url):
    return _strip_url_prefix(
        url, ("https://orcid.org/", "http://orcid.org/", "orcid.org/"))


def _strip_openalex_id(url):
    return _strip_url_prefix(
        url, ("https://openalex.org/", "http://openalex.org/", "openalex.org/"))


def _openalex_metrics(doi):
    """Return (cited_by_count, keywords, abstract, authorships,
    citations_by_year) or (None, [], None, [], [])."""
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.openalex.org/works/doi:" + qdoi
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if data is None:
        return None, [], None, [], []
    n = data.get("cited_by_count")
    if not isinstance(n, int):
        n = None
    kw = []
    for c in (data.get("concepts") or []):
        score = c.get("score")
        name = c.get("display_name")
        if isinstance(score, (int, float)) and score >= KEYWORD_SCORE_THRESHOLD and name:
            kw.append(name)
        if len(kw) >= KEYWORD_LIMIT:
            break
    abstract = _reconstruct_abstract(data.get("abstract_inverted_index"))
    authorships = []
    for a in (data.get("authorships") or []):
        auth = a.get("author") or {}
        insts = a.get("institutions") or []
        authorships.append({
            "name": auth.get("display_name"),
            "position": a.get("author_position"),
            "orcid": _strip_orcid(auth.get("orcid")),
            "openalex_id": _strip_openalex_id(auth.get("id")),
            "institution": (insts[0].get("display_name") if insts else None),
        })
    cby = []
    for r in (data.get("counts_by_year") or []):
        y = r.get("year")
        c = r.get("cited_by_count")
        if isinstance(y, int) and isinstance(c, int):
            cby.append({"year": y, "count": c})
    cby.sort(key=lambda r: r["year"])
    return n, kw, abstract, authorships, cby


def _normalize_doi(doi):
    """OpenAlex returns DOIs as full URLs; strip the prefix."""
    if not doi:
        return None
    s = doi.strip()
    low = s.lower()
    for p in ("https://doi.org/", "http://doi.org/"):
        if low.startswith(p):
            return s[len(p):]
    return s or None


def fetch_works_by_author(orcid=None, openalex_id=None, since=None, limit=50):
    """Return a list of works by the given author. Prefers ORCID, falls
    back to OpenAlex ID. `since` is an ISO date string (e.g. '2023-01-01')
    that, when set, restricts to works published on or after that date.
    Sorted most-recent first. Returns [] on failure / no match."""
    if not orcid and not openalex_id:
        return []
    if orcid:
        filt = "author.orcid:" + orcid
    else:
        filt = "author.id:" + openalex_id
    if since:
        filt += ",from_publication_date:" + since

    params = [
        ("filter", filt),
        ("sort", "publication_date:desc"),
        ("per_page", str(limit)),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)

    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=20)
    if data is None:
        return []

    results = []
    for w in (data.get("results") or []):
        # Author list (display names + structured authorships).
        authorships, author_names = [], []
        for a in (w.get("authorships") or []):
            auth = a.get("author") or {}
            insts = a.get("institutions") or []
            ash = {
                "name": auth.get("display_name"),
                "position": a.get("author_position"),
                "orcid": _strip_orcid(auth.get("orcid")),
                "openalex_id": _strip_openalex_id(auth.get("id")),
                "institution": (insts[0].get("display_name") if insts else None),
            }
            authorships.append(ash)
            if ash["name"]:
                author_names.append(ash["name"])
        primary = w.get("primary_location") or {}
        source = primary.get("source") or {}
        oa = w.get("open_access") or {}
        results.append({
            "openalex_id": _strip_openalex_id(w.get("id")),
            "doi": _normalize_doi(w.get("doi")),
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "publication_date": w.get("publication_date"),
            "type": w.get("type"),
            "citations": w.get("cited_by_count"),
            "journal": source.get("display_name"),
            "authors": author_names,
            "authorships": authorships,
            "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
            "is_oa": oa.get("is_oa"),
            "oa_url": oa.get("oa_url"),
        })
    return results


def fetch_author_profile(orcid=None, openalex_id=None):
    """Return {works_count, cited_by_count, h_index, i10_index, name,
    counts_by_year: [{year, works_count, cited_by_count}, ...]} or None.

    counts_by_year is OpenAlex's per-year totals (capped at ~10 years).
    Sorted oldest-first."""
    if not orcid and not openalex_id:
        return None
    if orcid:
        url = "https://api.openalex.org/authors/orcid:" + urllib.parse.quote(
            orcid, safe="")
    else:
        url = "https://api.openalex.org/authors/" + urllib.parse.quote(
            openalex_id, safe="")
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if data is None:
        return None
    summ = data.get("summary_stats") or {}
    cby = []
    for r in (data.get("counts_by_year") or []):
        y = r.get("year")
        if isinstance(y, int):
            cby.append({
                "year": y,
                "works_count": r.get("works_count") or 0,
                "cited_by_count": r.get("cited_by_count") or 0,
            })
    cby.sort(key=lambda r: r["year"])
    return {
        "name": data.get("display_name"),
        "works_count": data.get("works_count") or 0,
        "cited_by_count": data.get("cited_by_count") or 0,
        "h_index": summ.get("h_index"),
        "i10_index": summ.get("i10_index"),
        "counts_by_year": cby,
    }


def fetch_coauthors(orcid=None, openalex_id=None, limit=15):
    """Return a list of frequent co-authors as
    [{openalex_id, name, count}, ...] sorted by count desc. The target
    author themselves is filtered out. Returns [] on failure."""
    if not orcid and not openalex_id:
        return []
    if orcid:
        filt = "author.orcid:" + orcid
    else:
        filt = "author.id:" + openalex_id
    params = [
        ("filter", filt),
        ("group_by", "authorships.author.id"),
        ("per_page", "200"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=20)
    if data is None:
        return []

    target_id = (_strip_openalex_id(openalex_id) or openalex_id) if openalex_id else None
    out = []
    for g in (data.get("group_by") or []):
        oid = _strip_openalex_id(g.get("key")) or g.get("key")
        name = g.get("key_display_name")
        count = g.get("count") or 0
        if not oid or not name:
            continue
        if target_id and oid == target_id:
            continue
        out.append({"openalex_id": oid, "name": name, "count": count})
    # If we didn't have target_id (caller passed only ORCID), drop the top
    # row when it equals the largest plausible self-count: heuristic — if
    # the top entry's count is far above the next, it is the target.
    if not target_id and len(out) >= 2 and out[0]["count"] >= 2 * out[1]["count"]:
        out = out[1:]
    return out[:limit]


def _reconstruct_abstract(inv_idx):
    """Rebuild the plain-text abstract from OpenAlex's inverted-index
    representation: {word: [positions, ...], ...}.

    OpenAlex publishes abstracts this way (for licensing reasons) for
    most works. Returns the plain string, or None if absent / malformed."""
    if not inv_idx or not isinstance(inv_idx, dict):
        return None
    positions = []
    for word, idxs in inv_idx.items():
        if not isinstance(idxs, (list, tuple)):
            continue
        for idx in idxs:
            try:
                positions.append((int(idx), word))
            except (TypeError, ValueError):
                pass
    if not positions:
        return None
    positions.sort()
    return " ".join(word for _, word in positions) or None


def _crossref_count(doi):
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.crossref.org/works/" + qdoi
    data = _http_get_json(
        url,
        headers={"User-Agent": CROSSREF_UA, "Accept": "application/json"},
        timeout=15)
    if data is None:
        return None
    msg = data.get("message", {}) or {}
    n = msg.get("is-referenced-by-count")
    return int(n) if isinstance(n, int) else None
