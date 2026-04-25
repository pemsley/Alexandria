"""Citation-count lookup. OpenAlex first, CrossRef fallback.

Network calls are timeout-bounded and never raise — they return (None, None)
on any failure so the caller can decide whether to retry later.
"""

import json
import os
import re
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


_AUTHOR_WORKS_SORTS = {
    "recent": "publication_date:desc",
    "cited":  "cited_by_count:desc",
}


def fetch_works_by_author(orcid=None, openalex_id=None, since=None,
                          limit=50, sort="recent"):
    """Return a list of works by the given author. Prefers ORCID, falls
    back to OpenAlex ID. `since` is an ISO date string (e.g. '2023-01-01')
    that, when set, restricts to works published on or after that date.
    `sort` is one of:
      'recent' (default) — most-recently published first
      'cited'            — most-cited first
    Returns [] on failure / no match."""
    if not orcid and not openalex_id:
        return []
    sort_key = _AUTHOR_WORKS_SORTS.get(sort, _AUTHOR_WORKS_SORTS["recent"])
    if orcid:
        filt = "author.orcid:" + orcid
    else:
        filt = "author.id:" + openalex_id
    if since:
        filt += ",from_publication_date:" + since

    params = [
        ("filter", filt),
        ("sort", sort_key),
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
        # `oa.oa_url` is sometimes a direct PDF, sometimes a landing
        # page — useless for "Add to Archive". `best_oa_location` has
        # the disambiguated fields; `locations` lists *all* known
        # copies (publisher, PMC, repository, preprint server, ...) so
        # we can fall back when the primary fetch is blocked.
        bol = w.get("best_oa_location") or {}
        pdf_url = bol.get("pdf_url")
        landing_url = bol.get("landing_page_url") or oa.get("oa_url")

        # All known OA PDF URLs, best_oa_location first, then any
        # additional mirrors. De-duplicated, order preserved.
        pdf_urls = []
        if pdf_url:
            pdf_urls.append(pdf_url)
        for loc in (w.get("locations") or []):
            if not loc.get("is_oa"):
                continue
            u = loc.get("pdf_url")
            if u and u not in pdf_urls:
                pdf_urls.append(u)

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
            "oa_url": oa.get("oa_url"),     # back-compat
            "pdf_url": pdf_url,             # primary direct PDF (or None)
            "pdf_urls": pdf_urls,           # all OA mirrors, primary first
            "landing_url": landing_url,     # HTML article page (or None)
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


_CITED_BY_SORTS = {
    "recent": "publication_date:desc",
    "cited":  "cited_by_count:desc",
}


def fetch_cited_by(doi=None, openalex_id=None, sort="recent", limit=10):
    """Return the works that cite the given paper, as
    `[{openalex_id, doi, title, year, publication_date, journal,
       first_author, last_author, citations}, ...]`.
    `sort` is `'recent'` (default — newest citing papers first) or
    `'cited'` (most-cited citing papers first). Returns [] on
    failure / no DOI / no openalex_id.

    Implementation: OpenAlex's `cites:` filter wants a Work ID
    (`W12345...`), so when only a DOI is supplied we resolve it via
    `/works/doi:<doi>` first — that's one extra HTTP."""
    if not doi and not openalex_id:
        return []
    if not openalex_id:
        url = ("https://api.openalex.org/works/doi:"
               + urllib.parse.quote(doi, safe=""))
        if OPENALEX_MAILTO:
            url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
        data = _http_get_json(
            url,
            headers={"User-Agent": OPENALEX_UA,
                     "Accept": "application/json"},
            timeout=15)
        if not data:
            return []
        openalex_id = _strip_openalex_id(data.get("id")) or data.get("id")
        if not openalex_id:
            return []

    sort_key = _CITED_BY_SORTS.get(sort, _CITED_BY_SORTS["recent"])
    params = [
        ("filter", "cites:" + openalex_id),
        ("sort", sort_key),
        ("per_page", str(limit)),
        ("select",
         "id,doi,title,publication_year,publication_date,"
         "authorships,primary_location,cited_by_count"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=20)
    if not data:
        return []
    out = []
    for w in (data.get("results") or []):
        first, last = _author_first_last(w.get("authorships"))
        primary = w.get("primary_location") or {}
        src = primary.get("source") or {}
        out.append({
            "openalex_id": _strip_openalex_id(w.get("id")) or w.get("id"),
            "doi": _normalize_doi(w.get("doi")),
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "publication_date": w.get("publication_date"),
            "journal": src.get("display_name"),
            "first_author": first,
            "last_author": last,
            "citations": w.get("cited_by_count") or 0,
        })
    return out


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


# DOI prefixes used by preprint servers. Used to decide whether to
# bother looking up a "published version".
PREPRINT_DOI_PREFIXES = (
    "10.1101/",       # bioRxiv / medRxiv
    "10.48550/",      # arXiv
    "10.26434/",      # chemRxiv
    "10.21203/rs",    # Research Square
    "10.22541/au",    # Authorea
    "10.2139/ssrn",   # SSRN
    "10.31234/",      # PsyArXiv
    "10.31219/",      # OSF Preprints
    "10.20944/",      # Preprints.org
    "10.36227/",      # TechRxiv
)


def _author_first_last(authorships):
    """Pick first/last author display names from an OpenAlex authorships
    list. Falls back to first/last in publication order when position
    tags are missing."""
    first = last = None
    for a in (authorships or []):
        pos = (a.get("author_position") or "").lower()
        name = (a.get("author") or {}).get("display_name")
        if not name:
            continue
        if pos == "first" and not first:
            first = name
        elif pos == "last":
            last = name
    if first and last:
        return first, last
    names = [
        (a.get("author") or {}).get("display_name")
        for a in (authorships or [])
    ]
    names = [n for n in names if n]
    if not first and names:
        first = names[0]
    if not last and names:
        last = names[-1] if len(names) > 1 else names[0]
    return first, last


def fetch_related_works(doi=None, openalex_id=None, limit=12):
    """Return OpenAlex's `related_works` for a paper, resolved to
    [{openalex_id, doi, title, year, journal, first_author, last_author},
    ...] in the original order. Returns [] on failure."""
    if not doi and not openalex_id:
        return []

    # Step 1: fetch the work itself to get its related_works ids.
    if openalex_id:
        url = ("https://api.openalex.org/works/"
               + urllib.parse.quote(openalex_id, safe=""))
    else:
        url = ("https://api.openalex.org/works/doi:"
               + urllib.parse.quote(doi, safe=""))
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if data is None:
        return []
    rel_ids = []
    for r in (data.get("related_works") or []):
        sid = _strip_openalex_id(r) or r
        if sid and sid not in rel_ids:
            rel_ids.append(sid)
    if not rel_ids:
        return []
    rel_ids = rel_ids[:limit]

    # Step 2: one batched fetch using ids.openalex:W1|W2|...
    filt = "ids.openalex:" + "|".join(rel_ids)
    params = [
        ("filter", filt),
        ("per_page", str(len(rel_ids))),
        ("select",
         "id,doi,title,publication_year,authorships,primary_location"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url2 = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data2 = _http_get_json(
        url2,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=20)
    if data2 is None:
        return []

    by_id = {}
    for w in (data2.get("results") or []):
        wid = _strip_openalex_id(w.get("id")) or w.get("id")
        first, last = _author_first_last(w.get("authorships"))
        primary = w.get("primary_location") or {}
        src = primary.get("source") or {}
        by_id[wid] = {
            "openalex_id": wid,
            "doi": _normalize_doi(w.get("doi")),
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "journal": src.get("display_name"),
            "first_author": first,
            "last_author": last,
        }
    return [by_id[r] for r in rel_ids if r in by_id]


def is_preprint_doi(doi):
    if not doi:
        return False
    low = doi.lower()
    return any(low.startswith(p) for p in PREPRINT_DOI_PREFIXES)


def _surname(name):
    """Last whitespace-separated token of a name. Imperfect for
    compound surnames ("van der Waals" → "Waals") but fine for
    set-overlap matching."""
    if not name:
        return ""
    parts = name.strip().split()
    return parts[-1] if parts else ""


def find_published_version(title, author_names, preprint_doi=None):
    """Search OpenAlex for a journal-published version of a preprint by
    title + author overlap. Returns a dict {doi, title, journal, year,
    openalex_id, checked} or None.

    `author_names` is a list of full names (we only use the surnames
    for cross-checking)."""
    if not title or not author_names:
        return None
    # OpenAlex's title.search wants a clean phrase; strip punctuation.
    q_title = re.sub(r"\W+", " ", title).strip()
    if len(q_title) < 8:
        return None
    q_title = q_title[:200]

    params = [
        ("filter", "title.search:" + q_title + ",type:article"),
        ("per_page", "10"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)

    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if data is None:
        return None

    pre_surnames_lc = {_surname(n).lower()
                       for n in author_names if n and _surname(n)}
    if not pre_surnames_lc:
        return None
    pre_doi_lc = (preprint_doi or "").lower()

    for w in (data.get("results") or []):
        cand_doi = _normalize_doi(w.get("doi"))
        if not cand_doi:
            continue
        if cand_doi.lower() == pre_doi_lc:
            continue                                    # the preprint itself
        if is_preprint_doi(cand_doi):
            continue                                    # another preprint
        if w.get("type") != "article":
            continue                                    # dataset / book / ...
        cand_surnames = set()
        for a in (w.get("authorships") or []):
            n = (a.get("author") or {}).get("display_name") or ""
            sn = _surname(n).lower()
            if sn:
                cand_surnames.add(sn)
        if not (pre_surnames_lc & cand_surnames):
            continue                                    # no author overlap
        pl = w.get("primary_location") or {}
        src = pl.get("source") or {}
        return {
            "doi": cand_doi,
            "title": w.get("title"),
            "journal": src.get("display_name"),
            "year": w.get("publication_year"),
            "openalex_id": _strip_openalex_id(w.get("id")),
            "checked": today_iso(),
        }
    return None
