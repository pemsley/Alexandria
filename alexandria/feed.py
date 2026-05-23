"""Subscription feed: fetch newest articles for followed journals
and saved searches.

Two flavours, mirroring Wispar:
    * `journal_issn`: hit CrossRef `/works` filtered by ISSN(s),
      sorted by deposit date desc. This is the canonical "what
      did this journal publish lately" query.
    * `openalex_query`: hit OpenAlex `/works` with a free-text
      `search=`, sorted by `publication_date` desc.

The results are normalised into a single dict shape with
`doi`/`openalex_id`/`title`/`authors`/`journal`/`year`/
`published_date`/`abstract`/`is_oa`/`oa_url` so the caller
(see `index.upsert_discovered`) can persist them uniformly.

This module is deliberately UI-free; the browser wires it into
a background refresher thread alongside the existing citation
refresher.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import metrics
from .metrics import (
    OPENALEX_MAILTO, OPENALEX_UA, CROSSREF_UA, _http_get_json,
    _normalize_doi, _reconstruct_abstract, _strip_openalex_id,
)


# Google-Scholar style metadata convention — Springer-Nature,
# Wiley, OUP, IEEE, ACS, Sage, Cell Press, … all expose this on
# their landing pages. We only scrape this one tag; everything
# beyond it (publisher-specific OA-status extraction, full
# bibliographic harvest) is on the BACKLOG.
_CITATION_PDF_URL_RE = re.compile(
    r"""<meta\s+[^>]*name=["']citation_pdf_url["']\s+"""
    r"""[^>]*content=["']([^"']+)["']""",
    re.IGNORECASE)
# Some publishers put `content=` before `name=`.
_CITATION_PDF_URL_RE_ALT = re.compile(
    r"""<meta\s+[^>]*content=["']([^"']+)["']\s+"""
    r"""[^>]*name=["']citation_pdf_url["']""",
    re.IGNORECASE)


# Browser-ish UA. Many publishers and Cloudflare-shielded pages
# 403 a Python urllib UA on sight. Same posture as
# `author_works._download_pdf`.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch_landing_pdf_url(doi, timeout=12, max_bytes=512 * 1024):
    """Hit `https://doi.org/{doi}`, follow redirects, scrape the
    publisher landing page for `<meta name="citation_pdf_url">`.
    Returns the URL string or None.

    Used by `refresh_subscription` as a fallback when Unpaywall
    flagged a paper as OA but had no PDF URL for it — common for
    very-recently-published articles. We cap the read at
    `max_bytes` so a giant landing page doesn't blow up the
    feed refresher; the meta tag is always in <head> and 512 KB
    is plenty for that.

    Failures are silent — this is best-effort enrichment, not a
    critical path."""
    if not doi:
        return None
    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
    try:
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
    except (urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, OSError, ValueError):
        return None
    except Exception:
        return None
    if not body:
        return None
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return None
    m = _CITATION_PDF_URL_RE.search(text)
    if not m:
        m = _CITATION_PDF_URL_RE_ALT.search(text)
    if not m:
        return None
    return m.group(1).strip() or None


# Eight-digit ISSN with optional dash, case-insensitive checksum char.
_ISSN_RE = re.compile(r"^\d{4}-?\d{3}[\dxX]$")


FEED_FETCH_ROWS = 50

# How long a deposit must "rest" in CrossRef before we surface it
# in the feed. Publishers register the DOI before flipping the
# article's page live; in the meantime the publisher URL 404s.
# The lag varies (seconds to most of a day) but a few hours catches
# the worst of it without delaying actual research deposits much.
# Verified case: Nature `d41586-026-01518-4` deposited 07:02 UTC
# returned a Nature "Page not found" until the publisher caught up.
PUBLISH_LAG_HOURS = 6


def fetch_journal_articles(issns, limit=FEED_FETCH_ROWS,
                           publish_lag_hours=PUBLISH_LAG_HOURS):
    """Newest articles in the journal identified by `issns` (a
    list of ISSN strings; a journal may have multiple — e.g.
    Science has print 0036-8075 and online 1095-9203). Sorted
    by Crossref's `created` field, descending.

    Articles whose CrossRef `created` timestamp is younger than
    `publish_lag_hours` are dropped — the publisher hasn't had
    time to make the page live yet and showing them in the feed
    leads to dead links. We fetch with a wider page than `limit`
    so the post-filter still returns a useful slice."""
    if not issns:
        return []
    issn_filter = ",".join("issn:" + i.strip() for i in issns if i.strip())
    if not issn_filter:
        return []
    # Over-fetch to keep ~`limit` rows after the publish-lag filter.
    # Most days the filter discards a small percentage; right after
    # a publication burst (e.g. Nature's morning news cycle) it can
    # discard much more, hence the 2× cushion.
    fetch_rows = min(max(limit * 2, 50), 100)
    params = [
        ("filter", issn_filter),
        ("sort",   "created"),
        ("order",  "desc"),
        ("rows",   str(fetch_rows)),
        ("select", "DOI,title,issued,created,published-online,"
                   "published-print,author,container-title,abstract"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = ("https://api.crossref.org/works?"
           + urllib.parse.urlencode(params))
    data = _http_get_json(
        url,
        headers={"User-Agent": CROSSREF_UA,
                 "Accept": "application/json"},
        timeout=20)
    if not data:
        return []
    items = (data.get("message") or {}).get("items") or []
    cutoff_ms = None
    if publish_lag_hours and publish_lag_hours > 0:
        import time
        cutoff_ms = int((time.time() - publish_lag_hours * 3600) * 1000)
    out = []
    for it in items:
        if not it:
            continue
        if cutoff_ms is not None:
            ts = ((it.get("created") or {}).get("timestamp"))
            if isinstance(ts, (int, float)) and ts > cutoff_ms:
                continue
        out.append(_normalize_crossref_item(it))
        if len(out) >= limit:
            break
    return out


def fetch_openalex_query_articles(query, limit=FEED_FETCH_ROWS):
    """Free-text OpenAlex search, sorted by publication date desc.
    Used for topic subscriptions ("T cells", "ribosome
    structures", ...).

    Filters applied:
      * `type:article|review|preprint` — drops OpenAlex's datasets,
        book chapters, retractions, etc. which clutter a topic
        feed.
      * `to_publication_date:<today>` — OpenAlex stores some
        future-dated placeholders (e.g. 2050-01-01) for scheduled
        publications; without this cap they monopolise the head
        of a desc sort.
    """
    if not query or not query.strip():
        return []
    import datetime
    today = datetime.date.today().isoformat()
    filt = "type:article|review|preprint,to_publication_date:" + today
    params = [
        ("search",   query.strip()),
        ("filter",   filt),
        ("sort",     "publication_date:desc"),
        ("per_page", str(limit)),
        ("select",   "id,doi,title,publication_year,publication_date,"
                     "primary_location,authorships,abstract_inverted_index,"
                     "open_access"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = ("https://api.openalex.org/works?"
           + urllib.parse.urlencode(params))
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA,
                 "Accept": "application/json"},
        timeout=20)
    if not data:
        return []
    return [_normalize_openalex_item(w)
            for w in (data.get("results") or []) if w]


# CrossRef offers a free-form `query=` parameter too; we don't
# wire it as a subscription kind because OpenAlex's search is
# better-tokenised for topical interest. CrossRef stays the
# canonical source for the ISSN-scoped journal feed.


def _crossref_pick_date(item):
    """Pick the most informative date from a CrossRef item. Order:
    `published-online`, `published-print`, `issued`. Each is a
    `{"date-parts": [[y, m, d]]}` dict."""
    for k in ("published-online", "published-print", "issued"):
        v = item.get(k)
        if not v:
            continue
        parts = v.get("date-parts") or []
        if parts and parts[0]:
            return parts[0]
    return None


def _normalize_crossref_item(item):
    title = item.get("title") or []
    if isinstance(title, list):
        title = title[0] if title else None
    journal = item.get("container-title") or []
    if isinstance(journal, list):
        journal = journal[0] if journal else None
    date_parts = _crossref_pick_date(item)
    year = date_parts[0] if date_parts else None
    pub_date = None
    if date_parts:
        ymd = (date_parts + [None, None])[:3]
        if ymd[0] is not None:
            pub_date = "{:04d}".format(int(ymd[0]))
            if ymd[1] is not None:
                pub_date += "-{:02d}".format(int(ymd[1]))
                if ymd[2] is not None:
                    pub_date += "-{:02d}".format(int(ymd[2]))
    authors = []
    for a in (item.get("author") or []):
        given = a.get("given") or ""
        family = a.get("family") or ""
        full = (given + " " + family).strip()
        if full:
            authors.append(full)
    # CrossRef abstracts are JATS-flavoured XML; we strip the
    # outermost <jats:p> if present and otherwise hand the raw
    # string back. UI can render it lightly.
    abstract = item.get("abstract")
    if abstract and isinstance(abstract, str):
        abstract = abstract.strip()
    return {
        "doi":            _normalize_doi(item.get("DOI")),
        "openalex_id":    None,
        "title":          title,
        "authors":        authors,
        "journal":        journal,
        "year":           year,
        "published_date": pub_date,
        "abstract":       abstract,
        "is_oa":          False,    # CrossRef doesn't carry OA flag
        "oa_url":         None,
    }


def _normalize_openalex_item(w):
    pl = w.get("primary_location") or {}
    src = pl.get("source") or {}
    authors = []
    for a in (w.get("authorships") or []):
        n = (a.get("author") or {}).get("display_name")
        if n:
            authors.append(n)
    inv = w.get("abstract_inverted_index")
    abstract = _reconstruct_abstract(inv) if inv else None
    oa = w.get("open_access") or {}
    return {
        "doi":            _normalize_doi(w.get("doi")),
        "openalex_id":    _strip_openalex_id(w.get("id")),
        "title":          w.get("title"),
        "authors":        authors,
        "journal":        src.get("display_name"),
        "year":           w.get("publication_year"),
        "published_date": w.get("publication_date"),
        "abstract":       abstract,
        "is_oa":          bool(oa.get("is_oa")),
        "oa_url":         oa.get("oa_url"),
    }


def find_journal_by_name(query, limit=100):
    """Resolve a user-typed journal name (or ISSN) to a list of
    CrossRef journal records — each has `title`, `ISSN` (list),
    `publisher`. Used by the "Follow journal" dialog so the user
    can pick the right ISSN(s) rather than typing them by hand.

    When `query` looks like an ISSN we hit the by-ISSN endpoint
    directly. Otherwise we walk CrossRef's `/journals?query=`,
    pulling a wide page (default 100) and re-ranking. The
    substring/fuzzy matcher ranks by deposit volume, so popular
    flagship journals like `Science` get drowned out by long-tail
    sound-alikes (`ScienceAsia`, `ScienceBank`, ...); the real
    Science journal appears past row 25 on a query for "Science".
    Exact-title matches are pulled to the top, then prefix
    matches, then word-boundary substring matches, before falling
    back to CrossRef's own order.

    Returns [] on lookup failure; the caller can decide to show
    an error message."""
    if not query or not query.strip():
        return []
    q = query.strip()
    # ISSN-shaped query: skip the search and go straight to the
    # /journals/{issn} endpoint. Faster and avoids the ranking
    # problem entirely.
    if _ISSN_RE.match(q):
        issn_url = "https://api.crossref.org/journals/" + q
        data = _http_get_json(
            issn_url,
            headers={"User-Agent": CROSSREF_UA,
                     "Accept": "application/json"},
            timeout=15)
        if not data:
            return []
        msg = data.get("message") or {}
        issns = msg.get("ISSN") or []
        if isinstance(issns, str):
            issns = [issns]
        if not issns:
            return []
        return [{
            "title":     msg.get("title"),
            "issns":     [i for i in issns if i],
            "publisher": msg.get("publisher"),
        }]
    params = [
        ("query", q),
        ("rows",  str(limit)),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = ("https://api.crossref.org/journals?"
           + urllib.parse.urlencode(params))
    data = _http_get_json(
        url,
        headers={"User-Agent": CROSSREF_UA,
                 "Accept": "application/json"},
        timeout=15)
    if not data:
        return []
    raw = []
    for it in (data.get("message") or {}).get("items") or []:
        issn = it.get("ISSN") or []
        if isinstance(issn, str):
            issn = [issn]
        raw.append({
            "title":     it.get("title"),
            "issns":     [i for i in issn if i],
            "publisher": it.get("publisher"),
        })
    qlow = q.lower()

    def rank(entry):
        title = (entry.get("title") or "").lower()
        if title == qlow:
            return 0
        # "Science" before "ScienceAsia" before "Asian Science":
        # word-boundary prefix beats mid-word match.
        if title.startswith(qlow + " ") or title == qlow:
            return 1
        if title.startswith(qlow):
            return 2
        # Has the query as a whitespace-bounded word.
        if (" " + qlow + " ") in (" " + title + " "):
            return 3
        return 9

    raw.sort(key=rank)
    # Drop entries with no ISSN — they can't be followed.
    return [e for e in raw if e.get("issns")]


def refresh_subscription(conn, subscription, limit=FEED_FETCH_ROWS):
    """Fetch the latest articles for one subscription row and
    upsert them into `discovered`. Returns (n_fetched,
    n_new_rows). Does NOT touch `last_fetched` — callers do that
    via `index.mark_subscription_fetched` only when the fetch
    succeeded, so transient network failures retry on the next
    refresher pass.

    Newly-inserted rows are enriched in-place with Unpaywall OA
    status so the feed window can show "Open Access" without
    round-tripping at render time. CrossRef-sourced journal rows
    have no OA flag at all from the upstream API, and OpenAlex's
    `is_oa` can lag Unpaywall's — `fetch_oa_locations` is the
    canonical source. Only fired for *new* inserts to avoid
    re-querying every refresh."""
    from . import index, metrics
    kind = subscription["kind"]
    query = subscription["query"]
    if kind == "journal_issn":
        issns = [s.strip() for s in (query or "").split(",") if s.strip()]
        articles = fetch_journal_articles(issns, limit=limit)
    elif kind == "openalex_query":
        articles = fetch_openalex_query_articles(query, limit=limit)
    elif kind == "crossref_query":
        # Reserved for future use; not wired into the UI yet.
        articles = []
    else:
        return 0, 0
    new_count = 0
    sub_id = subscription["id"]
    for a in articles:
        if not index.upsert_discovered(conn, sub_id, a):
            continue
        new_count += 1
        # Enrich the freshly-inserted row with Unpaywall OA data.
        # Failures here are non-fatal — the row is already
        # persisted, the badge just won't appear.
        doi = a.get("doi")
        if not doi:
            continue
        try:
            unpw = metrics.fetch_oa_locations(doi)
        except Exception:
            unpw = None
        if not unpw:
            continue
        oa_status = unpw.get("oa_status")
        is_oa = bool(unpw.get("is_oa"))
        locs = unpw.get("locations") or []
        oa_url = locs[0]["pdf_url"] if locs else None
        # Unpaywall lag fallback: when Unpaywall says is_oa=True
        # but has no PDF URL (typical for very-recently-published
        # papers it hasn't crawled yet), scrape the publisher
        # landing page for `<meta name="citation_pdf_url">`. The
        # publisher tag is authoritative and immediate.
        if is_oa and not oa_url:
            try:
                landing_pdf = _fetch_landing_pdf_url(doi)
            except Exception:
                landing_pdf = None
            if landing_pdf:
                oa_url = landing_pdf
        try:
            index.update_discovered_oa(
                conn, sub_id, doi, is_oa, oa_url, oa_status)
        except Exception:
            pass
    return len(articles), new_count
