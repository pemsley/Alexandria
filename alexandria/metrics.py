"""Citation-count lookup. OpenAlex first, CrossRef fallback.

Network calls are timeout-bounded and never raise — they return (None, None)
on any failure so the caller can decide whether to retry later.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from .identity import maintainer_email

OPENALEX_MAILTO = maintainer_email()
OPENALEX_UA = os.environ.get(
    "ALEXANDRIA_OPENALEX_UA",
    "alexandria/0.1 (mailto:{})".format(OPENALEX_MAILTO))
CROSSREF_UA = os.environ.get(
    "ALEXANDRIA_CROSSREF_UA",
    "alexandria/0.1 (mailto:{})".format(OPENALEX_MAILTO))
EUROPEPMC_UA = os.environ.get(
    "ALEXANDRIA_EUROPEPMC_UA",
    "alexandria/0.1 (mailto:{})".format(OPENALEX_MAILTO))


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

# When a 429 response carries a Retry-After longer than this, give
# up immediately. The server is telling us to wait hours (OpenAlex
# hands out 33000-second Retry-After once the daily-quota credits
# are exhausted) — retrying inside the call just stalls the caller.
# Anything below this threshold is a normal short-term throttle
# and we keep retrying.
_HTTP_RETRY_AFTER_GIVE_UP_SECONDS = 60.0

# Session-wide circuit breaker for OpenAlex. When we see a hard 429
# (Retry-After > _HTTP_RETRY_AFTER_GIVE_UP_SECONDS) we set this to
# the monotonic time at which we'll retry — and *all* subsequent
# OpenAlex calls fail fast until then. Background refreshers check
# `openalex_paused_until()` between iterations and stop walking when
# they see it set, so the daily-quota-exhausted state doesn't
# translate into hours of futile retries clogging the UI.
_openalex_paused_until = 0.0  # monotonic seconds

# Most-recent `X-RateLimit-Remaining` we saw on a successful
# OpenAlex response. None until we've had at least one successful
# call this session. Background workers gate on this so they back
# off *before* triggering the breaker rather than after.
_openalex_credits_remaining = None  # int or None
_openalex_credits_seen_at = 0.0     # monotonic seconds


def openalex_paused_until():
    """Return monotonic time at which the OpenAlex circuit breaker
    releases, or 0.0 when it isn't tripped. Background workers
    should call this between iterations and back off when it's set.
    Foreground callers can still try (the next 429 will fail fast
    via the same gate inside _http_get_json)."""
    return _openalex_paused_until


def openalex_credits_remaining():
    """Most-recent X-RateLimit-Remaining count observed on an
    OpenAlex response this session, or None if we haven't seen
    one yet. Used by background workers to throttle proactively
    instead of waiting for a 429."""
    return _openalex_credits_remaining


def openalex_credits_below(buffer):
    """True when we know we're running low — observed credits
    less than `buffer`. Returns False when we have no
    observation yet (don't gate on unknown state)."""
    return (_openalex_credits_remaining is not None
            and _openalex_credits_remaining < buffer)


def _trip_openalex_breaker(seconds):
    """Set the circuit breaker for `seconds` from now. Capped at
    24 h so a wildly-large Retry-After doesn't lock us out
    indefinitely if the server is wrong."""
    global _openalex_paused_until
    seconds = min(max(float(seconds), 0.0), 86400.0)
    _openalex_paused_until = time.monotonic() + seconds


# --- CrossRef proactive rate-limit throttle -----------------------
# CrossRef returns X-Rate-Limit-Limit / X-Rate-Limit-Interval and asks
# callers to self-pace. We track the most-recent observed limit and a
# sliding window of request timestamps, sleeping before we'd exceed
# the budget. Guarded by a lock because the window is read-modify-write
# across the background daemon threads.
_CROSSREF_DEFAULT_LIMIT = 50
_CROSSREF_DEFAULT_INTERVAL = 1.0   # seconds

_crossref_limit = _CROSSREF_DEFAULT_LIMIT        # int
_crossref_interval = _CROSSREF_DEFAULT_INTERVAL  # float seconds
_crossref_request_times = []   # monotonic timestamps, oldest first
_crossref_lock = threading.Lock()

_INTERVAL_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_crossref_interval(raw):
    """Parse an X-Rate-Limit-Interval value ("1s", "10s", "1m", or a
    bare number) to seconds. Falls back to the default on missing /
    unparseable / non-positive input."""
    if not raw:
        return _CROSSREF_DEFAULT_INTERVAL
    s = str(raw).strip().lower()
    unit = 1.0
    if s and s[-1] in _INTERVAL_UNIT_SECONDS:
        unit = _INTERVAL_UNIT_SECONDS[s[-1]]
        s = s[:-1]
    try:
        val = float(s) * unit
    except (TypeError, ValueError):
        return _CROSSREF_DEFAULT_INTERVAL
    return val if val > 0 else _CROSSREF_DEFAULT_INTERVAL


def _note_crossref_rate(headers):
    """Update the observed CrossRef rate limit / interval from a
    response's headers. No-op for whichever field is absent or
    unparseable, so a missing header never clobbers a good value."""
    if not headers:
        return
    global _crossref_limit, _crossref_interval
    with _crossref_lock:
        raw_limit = headers.get("X-Rate-Limit-Limit")
        if raw_limit is not None:
            try:
                n = int(raw_limit)
                if n > 0:
                    _crossref_limit = n
            except (TypeError, ValueError):
                pass
        raw_interval = headers.get("X-Rate-Limit-Interval")
        if raw_interval is not None:
            _crossref_interval = _parse_crossref_interval(raw_interval)


def _crossref_wait_seconds(request_times, now, limit, interval):
    """Pure decision core: given recent request timestamps, how long
    (seconds) must we wait before issuing another request to stay under
    `limit` per `interval`? Returns 0.0 when under budget. Timestamps
    older than the live window are ignored."""
    kept = [t for t in request_times if t > now - interval]
    if len(kept) < limit:
        return 0.0
    return max(0.0, kept[0] + interval - now)


def _crossref_throttle():
    """Block (sleep) just long enough to keep CrossRef requests under
    the observed rate. Token-reservation: record our slot under the
    lock, then sleep outside it so threads don't serialise."""
    now = time.monotonic()
    with _crossref_lock:
        global _crossref_request_times
        _crossref_request_times = [
            t for t in _crossref_request_times if t > now - _crossref_interval]
        wait = _crossref_wait_seconds(
            _crossref_request_times, now, _crossref_limit, _crossref_interval)
        # Reserve our slot at the time we'll actually fire so concurrent
        # threads compute their wait against it.
        _crossref_request_times.append(now + wait)
    if wait > 0:
        time.sleep(wait)


def _note_openalex_credits(headers):
    """Stash `X-RateLimit-Remaining` from an OpenAlex response.
    No-op if the header is missing or unparseable."""
    if not headers:
        return
    raw = headers.get("X-RateLimit-Remaining")
    if raw is None:
        return
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return
    global _openalex_credits_remaining, _openalex_credits_seen_at
    _openalex_credits_remaining = n
    _openalex_credits_seen_at = time.monotonic()


def _is_openalex_url(url):
    return "openalex.org" in (url or "")


def _is_crossref_url(url):
    return "api.crossref.org" in (url or "")


class OpenAlexQuotaExhausted(Exception):
    """Raised by `_http_get_json` when a persistent HTTP 429 with a
    long Retry-After indicates the daily quota is exhausted, and the
    caller asked to be told (raise_on_quota=True). Interactive code
    paths use this to show a clear status message instead of an
    empty-results page."""


def _http_get_json(url, headers, timeout, raise_on_quota=False):
    """GET `url`, return parsed JSON dict, or None on persistent failure.

    Retries on HTTP 429 (Retry-After honoured, capped) and 5xx
    (exponential backoff). All other errors return None immediately.

    Honours the OpenAlex circuit breaker — once tripped by a
    hard 429 (Retry-After > 60s), subsequent OpenAlex calls fail
    instantly until the breaker times out. CrossRef and Unpaywall
    are unaffected.

    If `raise_on_quota` is True, a final 429 (or a call made while
    the OpenAlex breaker is already open) raises
    `OpenAlexQuotaExhausted` instead of returning None, so
    interactive callers can distinguish quota exhaustion from
    "no data"."""
    # Circuit-breaker gate: bail before opening a socket.
    if _is_openalex_url(url) and _openalex_paused_until > time.monotonic():
        if raise_on_quota:
            raise OpenAlexQuotaExhausted(
                "OpenAlex daily quota exhausted "
                "(HTTP 429 — try again later)")
        return None
    attempt = 0
    is_oa_url = _is_openalex_url(url)
    is_cr_url = _is_crossref_url(url)
    while True:
        # Proactively pace CrossRef so we stay under its advertised
        # rate rather than waiting for a 429. Inside the retry loop so
        # a retried call is paced too.
        if is_cr_url:
            _crossref_throttle()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # Capture credits remaining for the rate-limit
                # gate before reading the body — cheap, and the
                # body read could in principle fail.
                if is_oa_url:
                    _note_openalex_credits(resp.headers)
                if is_cr_url:
                    _note_crossref_rate(resp.headers)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if is_oa_url and e.headers is not None:
                _note_openalex_credits(e.headers)
            if is_cr_url and e.headers is not None:
                _note_crossref_rate(e.headers)
            retry_ok = e.code == 429 or 500 <= e.code < 600
            if not retry_ok or attempt >= _HTTP_MAX_RETRIES:
                if e.code == 429 and raise_on_quota:
                    raise OpenAlexQuotaExhausted(
                        "OpenAlex daily quota exhausted "
                        "(HTTP 429 — try again later)")
                return None
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(ra) if ra is not None else _HTTP_BACKOFF_BASE
                except (TypeError, ValueError):
                    delay = _HTTP_BACKOFF_BASE
                # Hard 429 with a long Retry-After: trip the
                # session breaker for OpenAlex and give up this
                # call immediately. No point sleeping 30 s, the
                # next call will 429 too.
                if (delay > _HTTP_RETRY_AFTER_GIVE_UP_SECONDS
                        and is_oa_url):
                    _trip_openalex_breaker(delay)
                    print("[metrics] OpenAlex rate-limited, pausing "
                          "for {:.0f} s".format(delay))
                    if raise_on_quota:
                        raise OpenAlexQuotaExhausted(
                            "OpenAlex daily quota exhausted "
                            "(HTTP 429 — try again later)")
                    return None
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
    citations_by_year, oa_title, oa_year, is_oa, oa_status,
    funders, grants).

    `oa_title` and `oa_year` are the OpenAlex Work's title and
    publication_year — the caller can cross-check these against
    PDF-extracted values to detect cross-contaminated OpenAlex
    records (rare but real: one paper's title/authors merged with
    another paper's DOI). They're None when the source is CrossRef
    or no data was returned.

    `is_oa` / `oa_status` come from OpenAlex's `open_access` block:
    a boolean plus a string in {`gold`, `hybrid`, `green`, `bronze`,
    `diamond`, `closed`}. None when CrossRef-only or no data.

    `citations_by_year` is a list of {year, count} dicts,
    oldest-first (capped by OpenAlex at ~10 years). `funders` is a
    deduped display-name list; `grants` is the per-award detail it
    derives from. Both empty when OpenAlex returned no grants block
    or the source is CrossRef. Any field may be None / [] depending
    on what OpenAlex / CrossRef returned."""
    if not doi:
        return None, None, [], None, [], [], None, None, None, None, [], []
    (n, kw, abstract, authorships, cby,
     oa_title, oa_year, is_oa, oa_status,
     funders, grants) = _openalex_metrics(doi)
    if n is not None:
        return (n, "openalex", kw, abstract, authorships, cby,
                oa_title, oa_year, is_oa, oa_status, funders, grants)
    # OpenAlex has no record (common for freshly-published DOIs). Fall
    # back to CrossRef — one HTTP call yields both the citation count
    # and enough to build an authorships list (name + ORCID +
    # affiliation). A later refresh, once OpenAlex indexes the work,
    # upgrades these to the richer OpenAlex version (openalex_id,
    # structured institutions). oa_title/oa_year carry the CrossRef
    # title/year so the importer's cross-contamination guard has
    # something to compare against.
    msg = _fetch_crossref_work_message(doi)
    if msg:
        cnt = msg.get("is-referenced-by-count")
        n = int(cnt) if isinstance(cnt, int) else None
        authorships = _crossref_authorships(msg)
        if n is not None or authorships:
            titles = msg.get("title") or []
            cr_title = (str(titles[0]).strip() or None) if titles else None
            cr_year = None
            issued = (msg.get("issued") or {}).get("date-parts") or []
            if issued and issued[0]:
                try:
                    cr_year = int(issued[0][0])
                except (ValueError, TypeError, IndexError):
                    cr_year = None
            return (n, "crossref", [], None, authorships, [],
                    cr_title, cr_year, None, None, [], [])
    return None, None, [], None, [], [], None, None, None, None, [], []


def fetch_citation_count(doi):
    """Backward-compatible wrapper returning just (count, source)."""
    n, src, *_ = fetch_metrics(doi)
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
    citations_by_year, title, year, is_oa, oa_status, funders,
    grants) or the all-empty equivalent. Title and year let the
    caller cross-check that the OpenAlex Work for `doi` is actually
    about the same paper — OpenAlex occasionally cross-contaminates
    records (one paper's title/authors/year merged with another
    paper's DOI/abstract/source), and a mismatch is the signal to
    fall back to the PDF-extracted metadata.

    `funders` is a deduped display-name list (order-preserving).
    `grants` is `[{funder, award_id}, ...]` — the per-award detail
    that `funders` is derived from. Both empty when OpenAlex
    returned no `grants` block."""
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.openalex.org/works/doi:" + qdoi
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if data is None:
        return None, [], None, [], [], None, None, None, None, [], []
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
    oa_title = data.get("title") or data.get("display_name")
    oa_year = data.get("publication_year")
    # OpenAlex `open_access` block: `{is_oa: bool, oa_status: str,
    # oa_url: str|None}`. Carry both fields so the card can render
    # an OA badge for paywalled DOIs that lack a CrossRef license
    # (Science / Nature / Cell typically don't deposit one).
    open_access = data.get("open_access")
    if open_access is None:
        is_oa, oa_status = None, None
    else:
        is_oa = bool(open_access.get("is_oa"))
        oa_status = open_access.get("oa_status") or None
    # Funders + grants. Same per-grant shape as `search_works`
    # (`funder` display-name + `award_id`); funder IDs / RORs are
    # dropped — `funders_json` is a deduped name list driven off the
    # same iteration so the two columns stay consistent.
    grants = []
    funders = []
    seen_funders = set()
    for g in (data.get("grants") or []):
        name = (g.get("funder_display_name") or "").strip()
        if not name:
            continue
        grants.append({
            "funder": name,
            "award_id": (g.get("award_id") or "").strip() or None,
        })
        if name not in seen_funders:
            seen_funders.add(name)
            funders.append(name)
    return (n, kw, abstract, authorships, cby,
            oa_title, oa_year, is_oa, oa_status, funders, grants)


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

        # License — OpenAlex's `primary_location.license` is a short
        # code like "cc-by-4.0", "cc-by-nc-nd", "publisher-specific-oa".
        # Cheaper than a per-row CrossRef call; matches the chip
        # vocabulary we use elsewhere via `classify_license_url`
        # when the code carries a CC URL fragment.
        license_code = primary.get("license")
        license_label = _label_from_openalex_license(license_code)
        # Grants: keep only display name + award id; drop the
        # OpenAlex funder ID URL noise. Most papers have 0-3
        # grants; the row UI caps the displayed list anyway.
        grants = []
        for g in (w.get("grants") or []):
            name = (g.get("funder_display_name") or "").strip()
            if not name:
                continue
            grants.append({
                "funder": name,
                "award_id": (g.get("award_id") or "").strip() or None,
            })
        # Topics: keep only the first (most-confident) topic's
        # display name. The full topic tree is interesting for
        # filtering later, but per-row we only need the label.
        topics_raw = w.get("topics") or []
        top_topic = None
        if topics_raw:
            top_topic = (topics_raw[0] or {}).get("display_name") or None
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
            "oa_status": oa.get("oa_status"),
            "license_code": license_code,
            "license_label": license_label,
            "oa_url": oa.get("oa_url"),     # back-compat
            "pdf_url": pdf_url,             # primary direct PDF (or None)
            "pdf_urls": pdf_urls,           # all OA mirrors, primary first
            "landing_url": landing_url,     # HTML article page (or None)
            "is_retracted": bool(w.get("is_retracted")),
            "is_paratext":  bool(w.get("is_paratext")),
            "fwci": w.get("fwci"),
            "grants": grants,
            "top_topic": top_topic,
        })
    return results


def fetch_author_profile(orcid=None, openalex_id=None):
    """Return {works_count, cited_by_count, h_index, i10_index, name,
    counts_by_year: [{year, works_count, cited_by_count}, ...],
    affiliations: [{display_name, openalex_id, year_min, year_max}, ...]}
    or None.

    counts_by_year is OpenAlex's per-year totals (capped at ~10 years),
    sorted oldest-first.

    affiliations is the author's full institution history collapsed
    to one row per institution with the year span condensed to
    (min, max). Sorted with the most-recent year_max first — the
    "where do they work *now*" question gets a fast answer."""
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
    aff_rows = []
    for af in (data.get("affiliations") or []):
        inst = af.get("institution") or {}
        name = inst.get("display_name")
        if not name:
            continue
        years = [y for y in (af.get("years") or []) if isinstance(y, int)]
        if not years:
            continue
        aff_rows.append({
            "display_name": name,
            "openalex_id": _strip_openalex_id(inst.get("id")),
            "year_min": min(years),
            "year_max": max(years),
        })
    aff_rows.sort(key=lambda r: r["year_max"], reverse=True)
    return {
        "name": data.get("display_name"),
        "works_count": data.get("works_count") or 0,
        "cited_by_count": data.get("cited_by_count") or 0,
        "h_index": summ.get("h_index"),
        "i10_index": summ.get("i10_index"),
        "counts_by_year": cby,
        "affiliations": aff_rows,
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


def _normalize_author_id(openalex_id):
    """Strip URL prefix from an OpenAlex author ID — accepts both
    `https://openalex.org/A5018808577` and `A5018808577`. Returns
    None for anything that doesn't end in an `A<digits>` token."""
    if not openalex_id:
        return None
    s = openalex_id.strip()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if not s.startswith("A") or not s[1:].isdigit():
        return None
    return s


# Title-keyword classifier for paper kind. Software-paper titles
# almost always advertise themselves: "Coot: model-building tools
# for…", "PROGRAM X for Y", "PHENIX: a software suite". Method
# papers usually carry an explicit method/algorithm/procedure word.
# Everything else is "idea" by default. Heuristic — wrong on some
# edge cases (review papers, mis-titled software papers), but right
# on the cases that dominate the citing-impact metric.
_PAPER_KIND_SOFTWARE_WORDS = (
    "program", "software", "package", "toolkit", "suite",
    "framework", "library", "server", "pipeline",
    "workflow", "platform",
    "tool for", "tools for", "tool that", "tools that",
    "deep learning", "machine learning",
)
_PAPER_KIND_METHOD_WORDS = (
    "method", "algorithm", "approach", "technique",
    "procedure", "protocol", "methodology", "formalism",
)


def classify_paper(title):
    """Return `'software'`, `'method'`, or `'idea'` for `title`.

    Used by `compute_citing_impact` to bucket an author's work so
    the citing-impact metric reports three honest pictures rather
    than collapsing them. A software-heavy career like Cowtan's
    has 65 k citing papers via the Coot paper, but those are
    "people who used the software" — a different signal from
    "people who built on the ideas". Surfacing all three lets the
    user judge.

    The heuristic looks for software / method words in the title.
    No signal → `'idea'`. Misses: review papers ("a review of
    methods…" → method), mis-titled software ("MultiCharge:
    quantum charge analysis" → idea, not software). The user-
    override path (per-paper `paper_kind` tag) is the escape
    hatch for those."""
    t = (title or "").lower()
    if not t:
        return "idea"
    if any(w in t for w in _PAPER_KIND_SOFTWARE_WORDS):
        return "software"
    if any(w in t for w in _PAPER_KIND_METHOD_WORDS):
        return "method"
    return "idea"


CITING_IMPACT_TOP_N_DEFAULT = 20


def compute_citing_impact(openalex_id, exclude_self_cites=True,
                          polite_delay=0.0,
                          top_n=CITING_IMPACT_TOP_N_DEFAULT):
    """Citing-paper impact for an author, bucketed by the kind
    of work being cited (software / method / idea).

    For each of the author's works, sum `cited_by_count` over
    every paper that cites it; group those sums under the cited
    work's kind. A paper that cites both a software work and a
    method work by this author counts in both buckets — the
    buckets are answers to three different questions ("did
    people cite this author as software? as method? as idea?")
    and dedup happens within each bucket, not across them.
    Optionally excludes self-cites.

    Returns
        {
          "software": {"total":  int,
                       "mean":   float,
                       "n_citing": int,
                       "n_works":  int},
          "method":   {...},
          "idea":     {...},
          "computed_at": iso-date,
        }
    or None when the author ID is malformed.

    `top_n` caps the per-author cost: we sort works by
    `cited_by_count` desc and only walk the citers of the top
    `top_n`. The metric's bucket totals come from "citers of
    this author's N most-cited papers" rather than "citers of
    all their papers". Tail papers contribute proportionally
    little to the metric anyway (Cowtan's Coot paper alone has
    more citers than the next 20 combined); the cost saving is
    5–10× on prolific authors. Setting `top_n=None` walks all
    works (~700 API calls for laureate-tier careers).

    `n_works` in each returned bucket reflects the *sampled*
    work count, not the author's total — so a software-heavy
    career capped at top_n=20 might show `software: n_works=10,
    method: n_works=3, idea: n_works=7`. Total works for the
    author is available separately via `fetch_author_profile`.

    Self-cite filtering happens server-side via OpenAlex's
    filter negation. Pagination via cursor."""
    aid = _normalize_author_id(openalex_id)
    if aid is None:
        return None

    # Step 1: every Work by this author. Pull `title` so we can
    # classify; everything else is for the metric. Cursor
    # pagination + minimal `select` keeps this to a couple of
    # calls for typical authors.
    works = []  # list of (wid, kind, cited_by_count)
    cursor = "*"
    while cursor:
        params = [
            ("filter", "author.id:" + aid),
            ("select", "id,title,cited_by_count"),
            ("per_page", "200"),
            ("cursor", cursor),
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
        if data is None:
            return None
        for w in (data.get("results") or []):
            wid = (w.get("id") or "").rsplit("/", 1)[-1]
            if not wid.startswith("W"):
                continue
            kind = classify_paper(w.get("title"))
            cbc = w.get("cited_by_count") or 0
            works.append((wid, kind, cbc))
        cursor = (data.get("meta") or {}).get("next_cursor")
        if cursor:
            time.sleep(polite_delay)

    # Top-N sampling: walk only the most-cited works. The metric is
    # dominated by these anyway — for Cowtan, top-20 by citation
    # count covers >95 % of all his citers. Skips a 5–10× cost
    # multiplier on prolific authors at negligible accuracy loss.
    if top_n is not None and len(works) > top_n:
        works.sort(key=lambda t: t[2], reverse=True)
        works = works[:top_n]

    # Initialise empty buckets so the return shape is consistent
    # even for authors with zero works.
    buckets = {
        "software": {"total": 0, "seen": set(), "n_works": 0},
        "method":   {"total": 0, "seen": set(), "n_works": 0},
        "idea":     {"total": 0, "seen": set(), "n_works": 0},
    }
    for _wid, kind, _cbc in works:
        buckets[kind]["n_works"] += 1

    if not works:
        return {
            "software": {"total": 0, "mean": 0.0, "n_citing": 0,
                         "n_works": 0},
            "method":   {"total": 0, "mean": 0.0, "n_citing": 0,
                         "n_works": 0},
            "idea":     {"total": 0, "mean": 0.0, "n_citing": 0,
                         "n_works": 0},
            "computed_at": today_iso(),
        }

    # Step 2: for each sampled Work, paginate through papers that
    # cite it and add to that Work's bucket. Per-bucket dedup so a
    # citing paper that hits multiple software works only counts
    # once for software; the same paper can land in multiple
    # buckets if it cites different kinds of works by this author.
    for wid, kind, _cbc in works:
        bucket = buckets[kind]
        seen = bucket["seen"]
        cursor = "*"
        while cursor:
            filt = "cites:" + wid
            if exclude_self_cites:
                filt += ",authorships.author.id:!" + aid
            params = [
                ("filter", filt),
                ("select", "id,cited_by_count"),
                ("per_page", "200"),
                ("cursor", cursor),
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
            if data is None:
                break
            for w in (data.get("results") or []):
                cid = (w.get("id") or "").rsplit("/", 1)[-1]
                if not cid.startswith("W") or cid in seen:
                    continue
                seen.add(cid)
                c = w.get("cited_by_count")
                if isinstance(c, int):
                    bucket["total"] += c
            cursor = (data.get("meta") or {}).get("next_cursor")
            if cursor:
                time.sleep(polite_delay)
        # Also sleep between distinct works — without this, an
        # author with many low-cite works (one page of citers
        # each) would burst back-to-back through the polite pool.
        if polite_delay:
            time.sleep(polite_delay)

    out = {"computed_at": today_iso()}
    for kind, b in buckets.items():
        n = len(b["seen"])
        out[kind] = {
            "total": b["total"],
            "mean": (b["total"] / n) if n else 0.0,
            "n_citing": n,
            "n_works": b["n_works"],
        }
    return out


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


def fetch_references(doi=None, openalex_id=None, limit=50):
    """Return `(refs, source)` for a paper's references in publication
    order.

    Tries OpenAlex's `referenced_works` first; if that's empty (the
    Work doesn't carry references), falls back to CrossRef's
    `reference` array on the same DOI — which the publisher
    deposited directly and is often present where OpenAlex is not.
    For CrossRef refs that themselves carry a DOI, we then
    batch-enrich via OpenAlex to get citation counts and richer
    titles, the same enrichment OpenAlex-source refs get.

    `source` is `'openalex'`, `'crossref'`, or `None` (for an
    empty result). Each ref dict has the shape:
      `{openalex_id, doi, title, year, publication_date, journal,
        first_author, last_author, citations}`.
    Returns `([], None)` on failure / no DOI."""
    if not doi and not openalex_id:
        return [], None

    # ---- OpenAlex path -------------------------------------------
    if not openalex_id:
        url = ("https://api.openalex.org/works/doi:"
               + urllib.parse.quote(doi, safe=""))
    else:
        url = ("https://api.openalex.org/works/"
               + urllib.parse.quote(openalex_id, safe=""))
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA,
                 "Accept": "application/json"},
        timeout=15)

    ref_ids = []
    if data:
        refs = data.get("referenced_works") or []
        for r in refs:
            sid = _strip_openalex_id(r) or r
            if sid and sid not in ref_ids:
                ref_ids.append(sid)

    if ref_ids:
        ref_ids = ref_ids[:limit]
        enriched = _enrich_works_by_openalex_id(ref_ids)
        # Preserve publication-order from referenced_works.
        return [enriched[r] for r in ref_ids if r in enriched], "openalex"

    # ---- CrossRef fallback ---------------------------------------
    # OpenAlex didn't have references for this paper; try the
    # publisher-deposited list on CrossRef. This catches the long
    # tail of journals where CrossRef has the bibliography but
    # OpenAlex hasn't ingested it yet.
    if not doi:
        return [], None
    cr_refs = _crossref_references(doi, limit=limit)
    if not cr_refs:
        return [], None
    # CrossRef refs that carry a DOI can be enriched via OpenAlex
    # in a single batched call — gives us the citation count and
    # cleaner titles for the popover. Refs without a DOI keep the
    # bare CrossRef structured fields (the user can still click
    # them; the popover's resolver will try a title search).
    dois = [r["doi"] for r in cr_refs if r.get("doi")]
    if dois:
        enriched_by_doi = _enrich_works_by_doi(dois)
        for r in cr_refs:
            d = r.get("doi")
            if d and d in enriched_by_doi:
                # OpenAlex enrichment overrides CrossRef's bare
                # fields where available — better titles, citation
                # counts, last_author, openalex_id.
                e = enriched_by_doi[d]
                for k in ("openalex_id", "title", "year",
                          "publication_date", "journal",
                          "first_author", "last_author", "citations"):
                    if e.get(k):
                        r[k] = e[k]
    return cr_refs, "crossref"


def _enrich_works_by_openalex_id(ref_ids):
    """Batched OpenAlex lookup of `[W<id>, ...]`. Returns
    `{openalex_id: <our standard ref dict>}`."""
    if not ref_ids:
        return {}
    filt = "ids.openalex:" + "|".join(ref_ids)
    params = [
        ("filter", filt),
        ("per_page", str(len(ref_ids))),
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
    by_id = {}
    if not data:
        return by_id
    for w in (data.get("results") or []):
        wid = _strip_openalex_id(w.get("id")) or w.get("id")
        first, last = _author_first_last(w.get("authorships"))
        primary = w.get("primary_location") or {}
        src = primary.get("source") or {}
        by_id[wid] = {
            "openalex_id": wid,
            "doi": _normalize_doi(w.get("doi")),
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "publication_date": w.get("publication_date"),
            "journal": src.get("display_name"),
            "first_author": first,
            "last_author": last,
            "citations": w.get("cited_by_count") or 0,
        }
    return by_id


def _enrich_works_by_doi(dois):
    """Batched OpenAlex lookup keyed by DOI. Returns
    `{doi: <our standard ref dict>}`. Used by the CrossRef
    references fallback to backfill citation counts and cleaner
    titles for refs that carry a DOI."""
    if not dois:
        return {}
    # OpenAlex's filter accepts up to 100 IDs per call via `|`.
    out = {}
    BATCH = 50
    for i in range(0, len(dois), BATCH):
        chunk = dois[i:i + BATCH]
        filt = "doi:" + "|".join(chunk)
        params = [
            ("filter", filt),
            ("per_page", str(len(chunk))),
            ("select",
             "id,doi,title,publication_year,publication_date,"
             "authorships,primary_location,cited_by_count"),
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
            continue
        for w in (data.get("results") or []):
            d = _normalize_doi(w.get("doi"))
            if not d:
                continue
            wid = _strip_openalex_id(w.get("id")) or w.get("id")
            first, last = _author_first_last(w.get("authorships"))
            primary = w.get("primary_location") or {}
            src = primary.get("source") or {}
            out[d] = {
                "openalex_id": wid,
                "doi": d,
                "title": w.get("title"),
                "year": w.get("publication_year"),
                "publication_date": w.get("publication_date"),
                "journal": src.get("display_name"),
                "first_author": first,
                "last_author": last,
                "citations": w.get("cited_by_count") or 0,
            }
    return out


def _crossref_references(doi, limit=50):
    """Fetch the publisher-deposited `reference` array from CrossRef
    for the paper at `doi`. Returns a list of entries in our standard
    ref-dict shape — DOI / title / year / journal / first_author —
    or `[]` if CrossRef has no reference list for this paper.

    CrossRef references vary wildly in completeness. Most have
    structured fields (`DOI`, `article-title`, `year`,
    `journal-title`, `author`); some only carry an `unstructured`
    raw bibliography string. We use `unstructured` as the title when
    nothing else is present, so the popover can still render a row
    and the user can decide whether to resolve via title search."""
    if not doi:
        return []
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.crossref.org/works/" + qdoi
    data = _http_get_json(
        url,
        headers={"User-Agent": CROSSREF_UA, "Accept": "application/json"},
        timeout=15)
    if not data:
        return []
    msg = data.get("message") or {}
    refs_raw = msg.get("reference") or []
    out = []
    for r in refs_raw:
        if not isinstance(r, dict):
            continue
        # Lowercase: CrossRef returns DOIs in their printed-on-paper
        # case (often uppercase IUCr IDs, e.g. "10.1107/S0907..."),
        # while OpenAlex normalises to lowercase. Keeping our DOIs
        # lowercase throughout means the OpenAlex-by-DOI enrichment
        # below matches against `enriched_by_doi[d]` correctly.
        ref_doi = _normalize_doi(r.get("DOI"))
        if ref_doi:
            ref_doi = ref_doi.lower()
        title = (r.get("article-title") or "").strip() or None
        if not title:
            unstr = (r.get("unstructured") or "").strip()
            if unstr:
                title = unstr
        year = None
        y = r.get("year")
        if y:
            try:
                year = int(str(y).strip()[:4])
            except (TypeError, ValueError):
                pass
        journal = (r.get("journal-title") or "").strip() or None
        author = (r.get("author") or "").strip() or None
        out.append({
            "openalex_id": None,
            "doi": ref_doi,
            "title": title,
            "year": year,
            "publication_date": None,
            "journal": journal,
            "first_author": author,
            "last_author": None,
            "citations": 0,
        })
        if len(out) >= limit:
            break
    return out


# Pattern → human label. Order matters: longer/more-specific
# patterns first, so `by-nc-sa` doesn't match the `by` rule.
_CC_LICENSE_PATTERNS = (
    ("creativecommons.org/licenses/by-nc-nd",   "CC-BY-NC-ND"),
    ("creativecommons.org/licenses/by-nc-sa",   "CC-BY-NC-SA"),
    ("creativecommons.org/licenses/by-nc",      "CC-BY-NC"),
    ("creativecommons.org/licenses/by-sa",      "CC-BY-SA"),
    ("creativecommons.org/licenses/by-nd",      "CC-BY-ND"),
    ("creativecommons.org/licenses/by",         "CC-BY"),
    ("creativecommons.org/publicdomain/zero",   "CC0"),
    ("creativecommons.org/publicdomain/mark",   "Public Domain"),
)


def _label_from_openalex_license(code):
    """Map OpenAlex's short `primary_location.license` code to the
    chip label vocabulary used elsewhere. Returns None when there's
    no recognisable license (the row then carries no chip rather
    than a misleading default)."""
    if not code:
        return None
    c = code.strip().lower()
    if not c:
        return None
    # OpenAlex codes look like "cc-by-4.0", "cc-by-nc-nd", "cc0",
    # "pd", "publisher-specific-oa", "implied-oa", "other-oa".
    if c == "cc0" or c.startswith("cc0-"):
        return "CC0"
    if c == "pd" or c == "public-domain":
        return "Public Domain"
    cc_map = (
        ("cc-by-nc-nd", "CC-BY-NC-ND"),
        ("cc-by-nc-sa", "CC-BY-NC-SA"),
        ("cc-by-nc",    "CC-BY-NC"),
        ("cc-by-sa",    "CC-BY-SA"),
        ("cc-by-nd",    "CC-BY-ND"),
        ("cc-by",       "CC-BY"),
    )
    for prefix, base in cc_map:
        if c.startswith(prefix):
            m = re.search(r"(\d+\.\d+)$", c)
            return "{}-{}".format(base, m.group(1)) if m else base
    return None


def classify_license_url(url):
    """Map a CrossRef license URL to a compact human label
    ("CC-BY-4.0", "CC-BY-NC", "© Elsevier", …) for the license
    chip. Returns None on empty input."""
    if not url:
        return None
    u = url.strip().lower()
    if not u:
        return None
    # CC licenses carry a version segment (`/by/4.0/`); pull it out
    # so the chip reads "CC-BY-4.0" rather than just "CC-BY".
    for pattern, base in _CC_LICENSE_PATTERNS:
        if pattern in u:
            m = re.search(r"/(\d+\.\d+)(/|$)", u)
            return "{}-{}".format(base, m.group(1)) if m else base
    # Publisher proprietary licenses — `elsevier.com/tdm/userlicense`,
    # `springernature.com/tdm`, `wiley.com/...`. Surface as
    # "© <publisher>" so the chip is informative without listing
    # every license URL ever minted.
    for host_hint, label in (
            ("elsevier.com",       "© Elsevier"),
            ("springer.com",       "© Springer"),
            ("springernature.com", "© Springer Nature"),
            ("nature.com",         "© Nature"),
            ("wiley.com",          "© Wiley"),
            ("onlinelibrary.wiley", "© Wiley"),
            ("acs.org",            "© ACS"),
            ("rsc.org",            "© RSC"),
            ("iucr.org",           "© IUCr"),
            ("aps.org",            "© APS"),
            ("ieee.org",           "© IEEE"),
            ("oup.com",            "© OUP"),
            ("cambridge.org",      "© Cambridge"),
            ("sagepub.com",        "© SAGE"),
            ("tandfonline.com",    "© T&F"),
            ("aip.org",            "© AIP"),
    ):
        if host_hint in u:
            return label
    return "© Publisher"


def _fetch_crossref_work_message(doi):
    """Internal helper: fetch `/works/{doi}` from CrossRef and
    return the `message` dict, or None. Centralises the URL and
    headers so the license / crossmark / count helpers don't drift
    apart and so a future caller that wants several fields can
    share a single HTTP call via `fetch_crossref_extras`."""
    if not doi:
        return None
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.crossref.org/works/" + qdoi
    data = _http_get_json(
        url,
        headers={"User-Agent": CROSSREF_UA, "Accept": "application/json"},
        timeout=15)
    if not data:
        return None
    return data.get("message") or {}


def _crossref_authorships(msg):
    """Build an authorships list (OpenAlex shape) from a CrossRef
    /works message. Used as a fallback when OpenAlex has no record for
    a freshly-published DOI. Each entry is
    {name, position, orcid, openalex_id, institution}; `openalex_id` is
    always None (CrossRef has no OpenAlex ID) and `institution` is the
    first non-empty affiliation string, if any. Position tags mirror
    OpenAlex's first/middle/last so position-aware consumers
    (`_author_first_last`) keep working."""
    authors = (msg or {}).get("author") or []
    out = []
    for a in authors:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = (given + " " + family).strip()
        if not name:
            continue
        institution = None
        for af in (a.get("affiliation") or []):
            nm = (af.get("name") or "").strip()
            if nm:
                institution = nm
                break
        out.append({
            "name": name,
            "position": "middle",
            "orcid": _strip_orcid(a.get("ORCID")),
            "openalex_id": None,
            "institution": institution,
        })
    if out:
        out[-1]["position"] = "last"
        out[0]["position"] = "first"   # wins for a single-author paper
    return out


def _license_from_message(msg):
    """Extract a `{url, label, content_version}` license dict from a
    CrossRef /works/{doi} message, or None. Pulled out of
    `fetch_license` so `fetch_crossref_extras` can share the HTTP
    call. `vor` (version of record) wins over `unspecified` over
    `am` over `tdm`."""
    licenses = (msg or {}).get("license") or []
    if not licenses:
        return None
    priority = {"vor": 0, "unspecified": 1, "am": 2, "tdm": 3}
    licenses_sorted = sorted(
        licenses,
        key=lambda l: priority.get((l.get("content-version") or "").lower(), 9))
    pick = licenses_sorted[0]
    pick_url = (pick.get("URL") or "").strip() or None
    label = classify_license_url(pick_url)
    if not label:
        return None
    return {
        "url":             pick_url,
        "label":           label,
        "content_version": pick.get("content-version") or None,
    }


def fetch_license(doi):
    """Fetch a CrossRef record for `doi` and extract a license entry
    suitable for a card chip. Returns `{url, label, content_version}`
    or None when CrossRef has no record / no license field."""
    msg = _fetch_crossref_work_message(doi)
    if msg is None:
        return None
    return _license_from_message(msg)


# CrossRef `updated-by[].type` → (severity rank, chip label).
# Severity rank decides which update wins when a paper has several
# (a paper can be both corrected and later retracted). Lower rank =
# more severe / more user-relevant.
_CROSSMARK_TYPES = {
    "retraction":             (0, "Retracted"),
    "withdrawal":             (0, "Withdrawn"),
    "partial_retraction":     (1, "Partial retraction"),
    "expression_of_concern":  (2, "Concern"),
    "removal":                (2, "Removed"),
    "correction":             (3, "Correction"),
    "corrigendum":            (3, "Corrigendum"),
    "erratum":                (3, "Erratum"),
    "clarification":          (4, "Clarification"),
    "addendum":               (4, "Addendum"),
    "new_version":            (5, "Updated"),
    "new_edition":            (5, "New edition"),
}


def _crossmark_from_message(msg):
    """Extract the most-severe Crossmark update from a CrossRef
    /works/{doi} message. Returns `{type, label, doi, year,
    severity}` or None.

    `updated-by` lists updates *pointing to* this paper — a
    retraction notice's DOI points back at the retracted paper via
    this field. A paper can have several entries (corrected, then
    later retracted); we surface the most severe, since "this
    paper was retracted" matters more to the reader than "this
    paper was also corrected"."""
    if not msg:
        return None
    entries = msg.get("updated-by") or []
    best = None  # (rank, dict)
    for e in entries:
        if not isinstance(e, dict):
            continue
        t = (e.get("type") or "").lower()
        rank_label = _CROSSMARK_TYPES.get(t)
        if not rank_label:
            continue
        rank, label = rank_label
        year = None
        dp = ((e.get("updated") or {}).get("date-parts") or [[]])[0]
        if dp:
            try:
                year = int(dp[0])
            except (TypeError, ValueError):
                pass
        candidate = {
            "type":     t,
            "label":    label,
            "doi":      (e.get("DOI") or "").lower() or None,
            "year":     year,
            "severity": rank,
        }
        if best is None or rank < best[0]:
            best = (rank, candidate)
    return best[1] if best else None


def fetch_crossmark(doi):
    """Return the most-severe Crossmark update pointing to `doi`, or
    None. Same shape as `_crossmark_from_message`."""
    msg = _fetch_crossref_work_message(doi)
    if msg is None:
        return None
    return _crossmark_from_message(msg)


def fetch_crossref_extras(doi):
    """Single CrossRef call that returns both license and crossmark
    info: `{license: ... | None, crossmark: ... | None}`. Used by
    the backfill pass so we don't hit /works/{doi} twice per row.
    Returns None when CrossRef has no record at all."""
    msg = _fetch_crossref_work_message(doi)
    if msg is None:
        return None
    return {
        "license":   _license_from_message(msg),
        "crossmark": _crossmark_from_message(msg),
    }


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


# Unpaywall is the upstream canonical OA-PDF-locator service from
# OurResearch (same shop as OpenAlex). Used as a fallback in the
# "Get PDF" path when OpenAlex's `oa_url` is missing or unreachable.
# Email required (their polite-pool convention, same as OpenAlex);
# 100 k requests/day is the documented ceiling. See BACKLOG entry
# "Unpaywall as a Get PDF fallback" for the full design.
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


# How we sort Unpaywall locations when handing them to the
# downloader: published version on the publisher's site is the
# closest copy to "the real paper"; preprint/submitted manuscripts
# are last resort. Lower number = higher preference.
_UNPAYWALL_VERSION_RANK = {
    "publishedVersion":  0,
    "acceptedVersion":   1,
    "submittedVersion":  2,
}
_UNPAYWALL_HOST_RANK = {
    "publisher":   0,
    "repository":  1,
}


def _unpaywall_location_rank(loc):
    """Sort key for an Unpaywall location dict. Tuple of
    (host_rank, version_rank) — both default high (worst) when
    the field is absent so unknown locations fall to the end."""
    h = _UNPAYWALL_HOST_RANK.get(loc.get("host_type"), 9)
    v = _UNPAYWALL_VERSION_RANK.get(loc.get("version"), 9)
    return (h, v)


def fetch_oa_locations(doi):
    """Look up `doi` in Unpaywall and return its OA locations as

        {
          "is_oa":      bool,
          "oa_status":  "gold"|"hybrid"|"green"|"bronze"|"closed",
          "locations":  [{
              "pdf_url":     str,
              "host_type":   "publisher"|"repository",
              "version":     "publishedVersion"|"acceptedVersion"
                             |"submittedVersion",
              "license":     str | None,
              "repository_institution": str | None,
          }, ...],
        }

    or None on lookup failure / unknown DOI. The `locations` list
    is sorted preferring publishedVersion@publisher > everything
    else (see `_unpaywall_location_rank`). Locations without a
    `url_for_pdf` are dropped — there's nothing to download.

    Used by the `Get PDF` flow in `browse.py` as a fallback when
    OpenAlex has no OA URL or every OpenAlex URL fails. Also
    useful for an OA-status chip on cards (the `oa_status` field
    is what Unpaywall is canonical for)."""
    if not doi:
        return None
    ndoi = _normalize_doi(doi)
    if not ndoi:
        return None
    if not OPENALEX_MAILTO:
        # Unpaywall requires an email — without one we can't
        # politely hit the endpoint. Same posture as the OpenAlex
        # helpers when MAILTO is unset.
        return None
    url = "{}/{}?email={}".format(
        UNPAYWALL_BASE,
        urllib.parse.quote(ndoi, safe=""),
        urllib.parse.quote(OPENALEX_MAILTO))
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if not data:
        return None
    out_locs = []
    seen_urls = set()
    raw_locs = list(data.get("oa_locations") or [])
    # `best_oa_location` is usually already in `oa_locations`, but
    # be defensive — some responses have it without the matching
    # entry.
    bol = data.get("best_oa_location")
    if bol:
        raw_locs = [bol] + raw_locs
    for loc in raw_locs:
        pdf_url = loc.get("url_for_pdf")
        if not pdf_url or pdf_url in seen_urls:
            continue
        seen_urls.add(pdf_url)
        out_locs.append({
            "pdf_url":   pdf_url,
            "host_type": loc.get("host_type"),
            "version":   loc.get("version"),
            "license":   loc.get("license"),
            "repository_institution": loc.get("repository_institution"),
        })
    out_locs.sort(key=_unpaywall_location_rank)
    return {
        "is_oa":     bool(data.get("is_oa")),
        "oa_status": data.get("oa_status"),
        "locations": out_locs,
    }


# --- PDBe: paper(s) associated with a PDB accession code -----------

_PDB_CODE_RE = re.compile(r"[0-9][a-z0-9]{3}")


def _parse_pdb_publications(data, code):
    """Pure parser: take PDBe's publications JSON for `code` and return
    a list of {doi, title, journal, year, authors, first_author,
    last_author, pubmed_id} dicts, primary citation first. Entries
    lacking a DOI are kept (the caller decides what to do)."""
    if not data or not code:
        return []
    pubs = data.get(code) or []
    out = []
    for p in pubs:
        title = (p.get("title") or "").strip() or None
        doi = (p.get("doi") or "").strip() or None
        pubmed_id = (p.get("pubmed_id") or "").strip() or None
        jinfo = p.get("journal_info") or {}
        journal = (jinfo.get("ISO_abbreviation")
                   or jinfo.get("pdb_abbreviation") or "").strip() or None
        year = jinfo.get("year")
        if not isinstance(year, int):
            try:
                year = int(year) if year else None
            except (TypeError, ValueError):
                year = None
        authors = [
            (a.get("full_name") or "").strip()
            for a in (p.get("author_list") or [])
            if (a.get("full_name") or "").strip()
        ]
        first_author = authors[0] if authors else None
        last_author = authors[-1] if len(authors) > 1 else None
        out.append({
            "doi": doi,
            "title": title,
            "journal": journal,
            "year": year,
            "authors": authors,
            "first_author": first_author,
            "last_author": last_author,
            "pubmed_id": pubmed_id,
        })
    return out


def fetch_pdb_publications(pdb_code):
    """Return PDBe's associated publications for a PDB accession code
    as a list (primary citation first), or [] on failure / unknown
    code. Validates the code shape before hitting the network."""
    code = (pdb_code or "").strip().lower()
    if not _PDB_CODE_RE.fullmatch(code):
        return []
    url = ("https://www.ebi.ac.uk/pdbe/api/pdb/entry/publications/" + code)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA,
                 "Accept": "application/json"},
        timeout=15)
    return _parse_pdb_publications(data, code)


def fetch_work_by_doi(doi):
    """Return one OpenAlex Work resolved by DOI as a normalised dict,
    or None on failure / unknown DOI.

    Same shape `fetch_references` and `fetch_cited_by` produce for
    each item, plus open-access fields the viewer's "Add + try PDF"
    flow needs to decide whether a download attempt is worthwhile.

    Used by the viewer when a clicked citation needs resolving on
    the spot — when the parsed bibliography entry already carries
    a DOI we hit OpenAlex directly with this; otherwise the caller
    runs `find_doi` first to produce one."""
    if not doi:
        return None
    url = ("https://api.openalex.org/works/doi:"
           + urllib.parse.quote(doi, safe=""))
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if not data:
        return None
    first, last = _author_first_last(data.get("authorships"))
    primary = data.get("primary_location") or {}
    src = primary.get("source") or {}
    best_oa = data.get("best_oa_location") or {}
    open_access = data.get("open_access") or {}
    authors = []
    for a in (data.get("authorships") or []):
        n = (a.get("author") or {}).get("display_name") or ""
        if n:
            authors.append(n)
    return {
        "openalex_id": _strip_openalex_id(data.get("id")) or data.get("id"),
        "doi": _normalize_doi(data.get("doi")),
        "title": data.get("title"),
        "year": data.get("publication_year"),
        "publication_date": data.get("publication_date"),
        "journal": src.get("display_name"),
        "first_author": first,
        "last_author": last,
        "authors": authors,
        "citations": data.get("cited_by_count") or 0,
        "is_oa": bool(open_access.get("is_oa")),
        "oa_url": best_oa.get("pdf_url") or best_oa.get("landing_page_url"),
    }


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


# --- "These Authors" discovery lens ----------------------------------
#
# Research Rabbit calls this "These Authors" — given a seed paper,
# what else has *this group of people* worked on together? We model
# the matcher as a small strategy table so the choice between
# possible matching rules is one line to flip:
#
#   * "overlap-2"   (DEFAULT) — fetch works for the seed's first and
#                   last authors, post-filter to candidates that
#                   share at least 2 authors with the seed. Catches
#                   lab papers (PI + several students), collaborator
#                   papers, and the grad-student-following-a-PI
#                   pattern. Lab-or-collaboration-shape; not
#                   anchored on any single role.
#
#   * "pi-anchored" — fetch works for only the seed's last author
#                   (typical PI position), post-filter to candidates
#                   that share at least 2 authors with the seed
#                   (last author + ≥1 other). Strict subset of
#                   overlap-2 hits where the PI is always involved;
#                   misses collaborator-only papers.
#
# Switching the default is the one-liner: change
# `DEFAULT_THESE_AUTHORS_RULE`. Surfacing the choice in Preferences
# is a future follow-on.


def _pick_first_and_last_authors_with_id(authorships):
    """Return up to 2 OpenAlex author IDs — the first author with
    an ID and the last author with an ID (different if possible).
    Used by the `overlap-2` strategy to bound the candidate pool
    we fetch."""
    ids = [a.get("openalex_id") for a in (authorships or [])
           if a and a.get("openalex_id")]
    if not ids:
        return []
    if len(ids) == 1:
        return [ids[0]]
    return [ids[0], ids[-1]]


def _pick_last_author_with_id(authorships):
    """Return the last author's OpenAlex ID as a single-element
    list, or []. Used by the `pi-anchored` strategy."""
    for a in reversed(authorships or []):
        if a and a.get("openalex_id"):
            return [a["openalex_id"]]
    return []


_THESE_AUTHORS_STRATEGIES = {
    "overlap-2": {
        "anchor_picker": _pick_first_and_last_authors_with_id,
        "min_overlap":   2,
        "description":   "first/last author + ≥1 other in common",
    },
    "pi-anchored": {
        "anchor_picker": _pick_last_author_with_id,
        "min_overlap":   2,
        "description":   "last (PI) author + ≥1 other in common",
    },
}
DEFAULT_THESE_AUTHORS_RULE = "overlap-2"


def fetch_papers_by_same_authors(authorships, seed_doi=None,
                                 seed_openalex_id=None,
                                 match_rule=None,
                                 per_anchor_limit=50,
                                 limit=20):
    """For a seed paper's authorships, find other papers that
    share enough of its authors to suggest the same group.
    Returns a list of dicts shaped like `fetch_related_works`
    plus an `overlap_count` field (the number of seed authors
    found in the candidate's authorship), sorted by overlap
    count descending then publication year descending.

    `match_rule` selects from `_THESE_AUTHORS_STRATEGIES`; falls
    back to `DEFAULT_THESE_AUTHORS_RULE` when None. Future
    callers can pass a different rule per call without changing
    the default."""
    rule = _THESE_AUTHORS_STRATEGIES.get(
        match_rule or DEFAULT_THESE_AUTHORS_RULE)
    if rule is None:
        return []
    anchor_ids = rule["anchor_picker"](authorships)
    if not anchor_ids:
        return []
    seed_author_ids = {a.get("openalex_id") for a in (authorships or [])
                       if a and a.get("openalex_id")}
    if not seed_author_ids:
        return []
    seed_doi_lc = _normalize_doi(seed_doi).lower() if seed_doi else None
    seed_oa_id = (seed_openalex_id or "").strip() or None

    # Fetch a batch of recent works per anchor, then post-filter.
    # Two anchors × per_anchor_limit candidates is enough that the
    # post-filter has a real selection to rank without exploding
    # the request count.
    candidates_by_id = {}
    for anchor in anchor_ids:
        works = fetch_works_by_author(
            openalex_id=anchor, limit=per_anchor_limit, sort="recent")
        for w in works:
            wid = w.get("openalex_id")
            if not wid:
                continue
            if wid in candidates_by_id:
                continue
            # Drop the seed itself.
            if seed_oa_id and wid == seed_oa_id:
                continue
            cdoi = (w.get("doi") or "").lower()
            if seed_doi_lc and cdoi == seed_doi_lc:
                continue
            # Compute author-set overlap.
            cand_ids = {ash.get("openalex_id")
                        for ash in (w.get("authorships") or [])
                        if ash.get("openalex_id")}
            overlap = len(seed_author_ids & cand_ids)
            if overlap < rule["min_overlap"]:
                continue
            w_with_overlap = dict(w)
            w_with_overlap["overlap_count"] = overlap
            candidates_by_id[wid] = w_with_overlap

    out = list(candidates_by_id.values())
    out.sort(key=lambda w: (w.get("overlap_count") or 0,
                            w.get("year") or 0),
             reverse=True)
    return out[:limit]


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


def find_doi(title, year=None, author_names=None, journal=None):
    """Search OpenAlex for a Work matching the given title (+ optional
    year, authors, journal) and return its DOI string, or None.

    Used to back-fill a DOI on BibTeX ghost entries that came in
    without one. We require at least one author-surname match to avoid
    grabbing an unrelated paper with a similar title.

    `author_names` is a list of full names (we use surnames only)."""
    if not title:
        return None
    # Drop BibTeX transliterations of symbols ("3-prime-end" really
    # means "3′-end") and similar fluff that won't be in the real title.
    cleaned = re.sub(r"\b(prime|hyphen)\b", " ", title, flags=re.I)
    q_title = re.sub(r"\W+", " ", cleaned).strip()
    if len(q_title) < 8:
        return None
    q_title = q_title[:200]

    filt = "title.search:" + q_title
    if year:
        filt += ",publication_year:{}".format(int(year))
    params = [
        ("filter", filt),
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

    surnames_lc = {_surname(n).lower()
                   for n in (author_names or []) if n and _surname(n)}
    journal_lc = (journal or "").lower().strip()

    for w in (data.get("results") or []):
        cand_doi = _normalize_doi(w.get("doi"))
        if not cand_doi:
            continue
        # Author-overlap gate (skip if the caller didn't give us authors).
        if surnames_lc:
            cand_surnames = set()
            for a in (w.get("authorships") or []):
                n = (a.get("author") or {}).get("display_name") or ""
                sn = _surname(n).lower()
                if sn:
                    cand_surnames.add(sn)
            if not (surnames_lc & cand_surnames):
                continue
        # Soft journal check: if both supplied and they're clearly
        # different, skip. Substring match is enough — OpenAlex source
        # names vary ("Science" vs "Science (American Association ...)").
        if journal_lc:
            cand_journal = (((w.get("primary_location") or {}).get("source")
                             or {}).get("display_name") or "").lower()
            if cand_journal and (journal_lc not in cand_journal
                                 and cand_journal not in journal_lc):
                continue
        return cand_doi
    return None


# Cache for `_resolve_journal_source_ids` — a session of citation
# resolution typically resolves dozens of "Acta Cryst. D" / "J. Mol.
# Biol." / etc. references, and there's no point re-asking OpenAlex
# every time. Keyed by the lowercase journal name.
_SOURCE_ID_CACHE = {}


# Common scientific-journal abbreviations, mapped to *every* full
# form they're known to expand to. Multiple candidates because the
# same abbreviation expands differently per journal: "Cryst." →
# "Crystallographica" in `Acta Cryst.` but "Crystallography" in
# `J. Appl. Cryst.`. OpenAlex's `/sources?search` is token-based
# and won't match abbreviations, so we run the cartesian product
# of expansions and union the source IDs.
_JOURNAL_ABBREVIATIONS = {
    "cryst": ["crystallographica", "crystallography"],
    "crystallogr": ["crystallography"],
    "j": ["journal"],
    "mol": ["molecular"],
    "biol": ["biology", "biological"],
    "biochem": ["biochemistry"],
    "chem": ["chemistry", "chemical"],
    "phys": ["physical", "physics"],
    "sci": ["science", "sciences"],
    "natl": ["national"],
    "acad": ["academy"],
    "proc": ["proceedings"],
    "comput": ["computational"],
    "commun": ["communications"],
    "newsl": ["newsletter"],
    "appl": ["applied"],
    "annu": ["annual"],
    "rev": ["review", "reviews"],
    "microbiol": ["microbiology"],
    "photochem": ["photochemistry"],
    "photobiol": ["photobiology"],
    "struct": ["structural"],
    "sect": ["section"],
    "enzymol": ["enzymology"],
    "med": ["medicine"],
    "exp": ["experimental"],
}


def _expand_journal_abbreviations(name):
    """Yield every expansion of `name` as a search-ready string —
    one per combination of alternate expansions. "J. Appl. Cryst."
    yields ["Journal Applied Crystallographica", "Journal Applied
    Crystallography"]. Tokens not followed by a period stay verbatim."""
    if not name:
        return [name] if name else []
    # Tokenise by whitespace, preserving the period-as-abbreviation marker.
    tokens = name.split()
    options = []  # list of [alt1, alt2, ...] per token
    for tok in tokens:
        bare = tok.rstrip(".,;:")
        if tok.endswith("."):
            alts = _JOURNAL_ABBREVIATIONS.get(bare.lower())
            if alts:
                options.append([a.title() for a in alts])
                continue
        options.append([bare])
    # Cartesian product, joined with spaces.
    out = [""]
    for opts in options:
        out = ["{} {}".format(prefix, alt).strip()
               for prefix in out for alt in opts]
    # Always include the original (token-stripped) too — sometimes
    # OpenAlex carries the raw name verbatim.
    raw = " ".join(tok.rstrip(".,;:") for tok in tokens).strip()
    if raw and raw not in out:
        out.append(raw)
    return out


def _resolve_journal_source_ids(journal_name):
    """Look up OpenAlex source IDs for a citation-style journal name
    abbreviation. Returns a (possibly empty) list of `S<id>` strings.

    A journal can have several OpenAlex source records — Acta D
    has two, one for "Biological Crystallography" (pre-2014) and
    one for "Structural Biology" (post-2014). We use
    `_journal_token_match` to keep only the sources whose printed
    name shares the citation's tokens (so `Acta Cryst. D` doesn't
    pull in section A / B / C / E / F)."""
    if not journal_name:
        return []
    key = journal_name.lower().strip()
    if key in _SOURCE_ID_CACHE:
        return _SOURCE_ID_CACHE[key]
    # OpenAlex's source search is token-based and full-text — it
    # won't match "Acta Cryst" against "Acta Crystallographica", and
    # the same abbreviation has multiple expansions ("J. Appl.
    # Cryst." → "Journal of Applied Crystallography", "Acta Cryst."
    # → "Acta Crystallographica"). Try each combination, union the
    # source IDs, then post-filter by token match against the
    # original journal name to drop unrelated sources.
    queries = _expand_journal_abbreviations(journal_name)
    seen_ids = set()
    ids = []
    for q in queries:
        q = re.sub(r"\W+", " ", q).strip()
        if not q:
            continue
        params = [("search", q), ("per_page", "10")]
        if OPENALEX_MAILTO:
            params.append(("mailto", OPENALEX_MAILTO))
        url = ("https://api.openalex.org/sources?"
               + urllib.parse.urlencode(params))
        data = _http_get_json(
            url,
            headers={"User-Agent": OPENALEX_UA,
                     "Accept": "application/json"},
            timeout=15)
        if data is None:
            continue
        for s in (data.get("results") or []):
            oa_id = s.get("id") or ""
            # OpenAlex IDs are full URLs; the filter wants the bare suffix.
            oa_id = oa_id.rsplit("/", 1)[-1] if oa_id else ""
            if not oa_id.startswith("S") or oa_id in seen_ids:
                continue
            if _journal_token_match(
                    journal_name, s.get("display_name") or ""):
                seen_ids.add(oa_id)
                ids.append(oa_id)
            if len(ids) >= 5:
                break
        if len(ids) >= 5:
            break
    _SOURCE_ID_CACHE[key] = ids
    return ids


def _journal_token_match(query, candidate):
    """True if `candidate` (a full OpenAlex source name) is the same
    journal as `query` (a citation-style abbreviation).

    Acta Cryst-style citations write "Acta Cryst. D"; OpenAlex
    stores "Acta Crystallographica Section D Biological
    Crystallography". A naive substring check fails on the
    abbreviation, so we tokenise both, then require every query
    token to match an exact or prefix candidate token. Single-
    character tokens (the section letter "D") must match exactly so
    we don't merge sections A/B/C/D/E/F."""
    if not query or not candidate:
        return False
    q_words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    c_words = [w for w in re.split(r"[^a-z0-9]+", candidate.lower()) if w]
    if not q_words or not c_words:
        return False
    # Common stop-words that shouldn't carry the match. "of", "and",
    # "the" appear in too many journal names to be informative.
    stops = {"of", "and", "the", "for", "in", "a", "an"}
    informative = [w for w in q_words if w not in stops]
    if not informative:
        return False
    c_set = set(c_words)
    for qw in informative:
        if qw in c_set:
            continue
        # Prefix-match against any candidate word, regardless of qw
        # length. Lets short abbreviations like "J." match "Journal"
        # (qw="j" prefix of "journal") while still keeping "Acta
        # Cryst. D" away from "Acta Cryst. A" (qw="d" doesn't match
        # "a" or "foundations").
        if any(cw.startswith(qw) for cw in c_words):
            continue
        return False
    return True


def find_doi_by_author_year(surname, year, journal=None):
    """Search OpenAlex for a Work by first-author surname + publication
    year + journal token-match, returning the DOI string or None.

    Used to resolve Acta-Cryst-style bibliography entries that carry
    no title — citation format is `Surname, I. (YYYY). J. Vol, Pages.`
    so `find_doi` (which is title-driven) has nothing to search on.

    Returns None when no candidate matches BOTH surname-as-first-
    author AND journal. Falling back to "first-author alone with no
    journal check" picks unrelated papers (an unrelated psychology
    paper with the same surname+year would win), which is worse than
    returning None and surfacing "couldn't resolve" to the user."""
    if not surname or not year:
        return None
    surname_clean = re.sub(r"\W+", " ", surname).strip()
    if not surname_clean:
        return None
    # OpenAlex's `raw_author_name.search` matches the author name as
    # printed on the paper, which is what citation surnames map to.
    # `authorships.author.display_name.search` is *not* a valid
    # filter (returns 400) — use this one instead.
    # Year ± 1 because online-vs-print publication years often
    # disagree by a year — Sheldrick's 2008 SHELX paper (Acta A vol
    # 64, 2008) is `publication_year: 2007` in OpenAlex (online
    # December 2007). Without the tolerance these "early/late
    # online" cases all fail to resolve.
    y = int(year)
    year_filt = "publication_year:{}|{}|{}".format(y - 1, y, y + 1)
    filt_parts = [
        "raw_author_name.search:{}".format(surname_clean),
        year_filt,
    ]
    # When the journal is known, resolve to its OpenAlex source ID(s)
    # and filter on those. Without this, common surnames (Cohen,
    # Smith) drown the right paper under hundreds of irrelevant
    # same-year hits and the in-memory journal token check on the
    # top-25 results misses the match. With it, we typically get
    # 0–5 candidates and the right one's right there.
    source_ids = _resolve_journal_source_ids(journal) if journal else []
    if source_ids:
        filt_parts.append(
            "primary_location.source.id:" + "|".join(source_ids))
    filt = ",".join(filt_parts)
    params = [
        ("filter", filt),
        ("per_page", "25"),
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

    surname_lc = surname_clean.lower()
    candidates = data.get("results") or []
    if not candidates:
        return None

    def _surname_matches_first_author(w):
        for a in (w.get("authorships") or []):
            pos = a.get("author_position") or "middle"
            if pos != "first":
                continue
            n = (a.get("author") or {}).get("display_name") or ""
            sn = _surname(n).lower()
            if sn == surname_lc:
                return True
            # `_surname` extracts the last whitespace-separated token,
            # which works for "First Last" but not for hyphenated /
            # particle-prefixed surnames ("Cohen-Luria", "Van Dijk")
            # or all-caps display names. Fall back to a substring
            # check on the first author's name only — keeps strict
            # first-author scoping while tolerating display-name
            # quirks.
            if surname_lc in n.lower():
                return True
            return False
        return False

    for w in candidates:
        if not _surname_matches_first_author(w):
            continue
        cand_journal = (((w.get("primary_location") or {}).get("source")
                         or {}).get("display_name") or "")
        if not _journal_token_match(journal, cand_journal):
            continue
        doi = _normalize_doi(w.get("doi"))
        if doi:
            return doi
    return None


def _published_version_via_crossref_relation(preprint_doi):
    """Look up `preprint_doi` in CrossRef and walk `message.relation`
    for an `is-preprint-of` link. Returns a `find_published_version`-
    shaped dict (DOI + title + journal + year + openalex_id +
    checked) or None when the field is absent or empty.

    Publisher-deposited and authoritative when present, which beats
    the title-search heuristic — preprints with later journal-
    version changes (renumbered figures, new co-author, edited
    title) can defeat the search by overlap alone. CrossRef tracks
    the relationship directly via the `relation` field on either
    side."""
    if not preprint_doi:
        return None
    qdoi = urllib.parse.quote(preprint_doi, safe="")
    data = _http_get_json(
        "https://api.crossref.org/works/" + qdoi,
        headers={"User-Agent": CROSSREF_UA, "Accept": "application/json"},
        timeout=15)
    if not data:
        return None
    msg = data.get("message") or {}
    rel = msg.get("relation") or {}
    # `is-preprint-of` carries the journal-published version we
    # want. `is-version-of` and `has-version` are more general
    # (corrections, dataset versions); we ignore them here.
    candidates = rel.get("is-preprint-of") or []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        if entry.get("id-type") != "doi":
            continue
        target = (entry.get("id") or "").strip().lower()
        if not target:
            continue
        # Enrich via OpenAlex so the caller gets the same dict
        # shape it would from the OpenAlex title-search path
        # (journal name + OpenAlex Work ID).
        oa_url = ("https://api.openalex.org/works/doi:"
                  + urllib.parse.quote(target, safe="")
                  + ("?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
                     if OPENALEX_MAILTO else ""))
        oa = _http_get_json(
            oa_url,
            headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
            timeout=15)
        if oa:
            pl = oa.get("primary_location") or {}
            src = pl.get("source") or {}
            return {
                "doi":         _normalize_doi(oa.get("doi")) or target,
                "title":       oa.get("title"),
                "journal":     src.get("display_name"),
                "year":        oa.get("publication_year"),
                "openalex_id": _strip_openalex_id(oa.get("id")),
                "checked":     today_iso(),
            }
        # OpenAlex doesn't know this DOI yet (rare but real for
        # very fresh publisher releases). Return the DOI alone —
        # the caller can still hand it to the user.
        return {
            "doi":         target,
            "title":       None,
            "journal":     None,
            "year":        None,
            "openalex_id": None,
            "checked":     today_iso(),
        }
    return None


def find_published_version(title, author_names, preprint_doi=None):
    """Search OpenAlex for a journal-published version of a preprint by
    title + author overlap. Returns a dict {doi, title, journal, year,
    openalex_id, checked} or None.

    `author_names` is a list of full names (we only use the surnames
    for cross-checking).

    When `preprint_doi` is supplied, we ask CrossRef first via the
    publisher-deposited `relation.is-preprint-of` field — that's
    authoritative and beats the title-search heuristic. The
    OpenAlex title-search path remains the fallback for preprints
    whose publisher hasn't deposited the relation."""
    if preprint_doi:
        viacross = _published_version_via_crossref_relation(preprint_doi)
        if viacross:
            return viacross

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


# ----------------------------------------------------------------------
# Discovery: free-form OpenAlex search by author / topic.
# ----------------------------------------------------------------------

def resolve_institution(query, limit=5):
    """Resolve an institution name (free text, e.g. "Stanford") to one
    or more OpenAlex Institution dicts, ordered by relevance. Caller
    can present these to the user when ambiguous, or just take the top
    hit. Returns [] on failure."""
    if not query:
        return []
    params = [
        ("search", query),
        ("per_page", str(max(1, min(int(limit), 25)))),
        ("select", "id,display_name,country_code,type,works_count,cited_by_count"),
    ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/institutions?" + urllib.parse.urlencode(params)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=15)
    if not data:
        return []
    out = []
    for i in (data.get("results") or []):
        out.append({
            "openalex_id": _strip_openalex_id(i.get("id")) or i.get("id"),
            "display_name": i.get("display_name"),
            "country_code": i.get("country_code"),
            "type": i.get("type"),
            "works_count": i.get("works_count") or 0,
            "cited_by_count": i.get("cited_by_count") or 0,
        })
    return out


def search_authors(name=None, institution=None, orcid=None, limit=15):
    """Return a list of candidate authors for the Discover dialog.

    `name` (free-text, e.g. "Clyde A. Smith") is the author search;
    `institution` (free-text, e.g. "Stanford") narrows by an
    institution appearing anywhere in the author's affiliation
    history; `orcid` is a fast-path that bypasses name search and
    fetches the single matching author. Returns [] on failure / no
    matches.

    Each result dict has the shape consumed by the Discover UI:
    `{openalex_id, orcid, display_name, last_known_institution,
    works_count, cited_by_count, top_topic, top_topic_id,
    matched_institution}` (the last is set when an institution
    constraint resolved to a specific OpenAlex Institution).

    Implementation note: OpenAlex doesn't support free-text
    filtering on affiliation display names, so an institution
    string is first resolved to an OpenAlex Institution ID, and
    the author search then filters on that ID. Top hit wins —
    "Stanford" maps to Stanford University, not SLAC. A v1
    follow-up would surface the multi-match case in the UI.

    Filtering: callers should drop records with `works_count == 0`
    if they want to hide OpenAlex's stub records.
    """
    select = ("id,display_name,orcid,works_count,cited_by_count,"
              "last_known_institutions,topics")
    matched_institution = None
    if orcid:
        # ORCID fast-path: single-author lookup. OpenAlex normalises
        # ORCIDs internally; pass either bare digits or full URL.
        params = [
            ("filter", "orcid:" + orcid),
            ("select", select),
        ]
    else:
        if not name:
            return []
        # Use `display_name.search:` rather than the top-level
        # `search=` parameter. The latter also matches against
        # `display_name_alternatives`, where OpenAlex sometimes
        # stores whole author-list strings parsed from a paper's
        # metadata (e.g. Venter's record carries
        # "Venter, J. Craig; Smith, Hamilton O.; Hutchison, III,
        # Clyde A.; Gibson, Daniel G." as an alternative name).
        # That made unrelated co-authors surface in name searches —
        # tightening to `display_name.search` cuts most of it.
        filt = ["display_name.search:" + name]
        if institution:
            inst_hits = resolve_institution(institution, limit=1)
            if not inst_hits:
                return []   # institution didn't resolve — empty result
            matched_institution = inst_hits[0]
            filt.append(
                "affiliations.institution.id:"
                + matched_institution["openalex_id"])
        params = [
            ("per_page", str(max(1, min(int(limit), 50)))),
            ("select", select),
            ("filter", ",".join(filt)),
        ]
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/authors?" + urllib.parse.urlencode(params)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=20,
        raise_on_quota=True)
    if not data:
        return []
    out = []
    for a in (data.get("results") or []):
        insts = a.get("last_known_institutions") or []
        topics = a.get("topics") or []
        top = topics[0] if topics else {}
        out.append({
            "openalex_id": _strip_openalex_id(a.get("id")) or a.get("id"),
            "orcid": _strip_orcid(a.get("orcid")),
            "display_name": a.get("display_name"),
            "last_known_institution":
                ", ".join(i.get("display_name", "") for i in insts) or None,
            "works_count": a.get("works_count") or 0,
            "cited_by_count": a.get("cited_by_count") or 0,
            "top_topic": top.get("display_name"),
            "top_topic_id": _strip_openalex_id(top.get("id")) if top else None,
            "matched_institution": matched_institution,
        })
    return out


_WORKS_SEARCH_SORTS = {
    "relevance": "relevance_score:desc",
    "cited":     "cited_by_count:desc",
    "recent":    "publication_date:desc",
}


def search_works(query, limit=25, sort="relevance", year_min=None,
                 require_doi=True, search_field="any"):
    """Free-text search across OpenAlex Works. Used by the Discover
    dialog's "By topic" and "By title" tabs.

    `search_field`:
      * `"any"` (default) — top-level `search=` parameter; matches
        title + abstract + fulltext (when indexed). Right for
        topic / keyword queries.
      * `"title"` — `filter=title.search:` instead; matches the
        title field only. Right for "find me this specific paper"
        queries where the user knows (most of) the title.

    Result shape matches `fetch_cited_by` / `fetch_references` so
    `_build_related_row` in browse.py renders them unchanged.
    Returns [] on failure / no matches.

    `require_doi=True` (the default) asks OpenAlex to only return
    works with a DOI — internal reports / theses / dataset records
    without a DOI are noise for the Discover flow because the "Add
    to library" path uses DOI as the de-duplication key. Pass
    False if a future caller wants the raw response."""
    if not query:
        return []
    sort_key = _WORKS_SEARCH_SORTS.get(sort, _WORKS_SEARCH_SORTS["relevance"])
    filt = []
    if year_min is not None:
        # OpenAlex doesn't take `>=` on `publication_year`; use the
        # `from_publication_date` cutoff instead (Jan 1 of year_min).
        try:
            filt.append("from_publication_date:{}-01-01".format(int(year_min)))
        except (TypeError, ValueError):
            pass
    if require_doi:
        filt.append("has_doi:true")
    if search_field == "title":
        filt.append("title.search:" + query)
    params = [
        ("sort", sort_key),
        ("per_page", str(max(1, min(int(limit), 50)))),
        ("select",
         "id,doi,title,publication_year,publication_date,"
         "authorships,primary_location,cited_by_count,open_access,"
         "best_oa_location"),
    ]
    if search_field != "title":
        params.insert(0, ("search", query))
    if filt:
        params.append(("filter", ",".join(filt)))
    if OPENALEX_MAILTO:
        params.append(("mailto", OPENALEX_MAILTO))
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = _http_get_json(
        url,
        headers={"User-Agent": OPENALEX_UA, "Accept": "application/json"},
        timeout=20,
        raise_on_quota=True)
    if not data:
        return []
    out = []
    for w in (data.get("results") or []):
        first, last = _author_first_last(w.get("authorships"))
        primary = w.get("primary_location") or {}
        src = primary.get("source") or {}
        best_oa = w.get("best_oa_location") or {}
        open_access = w.get("open_access") or {}
        authors = []
        for a in (w.get("authorships") or []):
            n = (a.get("author") or {}).get("display_name") or ""
            if n:
                authors.append(n)
        out.append({
            "openalex_id": _strip_openalex_id(w.get("id")) or w.get("id"),
            "doi": _normalize_doi(w.get("doi")),
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "publication_date": w.get("publication_date"),
            "journal": src.get("display_name"),
            "first_author": first,
            "last_author": last,
            "authors": authors,
            "citations": w.get("cited_by_count") or 0,
            "is_oa": bool(open_access.get("is_oa")),
            "oa_url": best_oa.get("pdf_url") or best_oa.get("landing_page_url"),
        })
    return out
