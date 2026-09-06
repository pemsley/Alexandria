"""Open-access PDF fetcher.

Tries publisher / repository PDF URLs from OpenAlex, then Unpaywall,
then EuropePMC. EuropePMC is the load-bearing third source: it
serves PMC-deposited PDFs of NIH/UKRI-funded papers including ones
fronted by Cloudflare-protected publisher pages (Nature, Cell, AAAS
journals), so it routinely succeeds where the publisher download
gets a TLS-fingerprint 403.

GTK-free. Lives outside `author_works.py` (which holds the old GUI
copy of the download helper) so the MCP server's venv can import
it without pulling in PyGObject."""

import inspect
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import metrics


# ---- low-level: single URL → file -----------------------------------

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def human_size(n):
    """Byte count as a short human string. Used in progress lines,
    where "28.3 MB" reads and "29684721" does not."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("{:.0f} {}" if unit == "B" or n >= 100
                    else "{:.1f} {}").format(n, unit)
        n /= 1024
    return "{:.1f} GB".format(n)


def host_of(url):
    """The hostname to name in a progress line — what the user
    recognises about a URL, without the tracking-length path."""
    try:
        return urllib.parse.urlparse(url).netloc or url
    except Exception:
        return url


def _report(on_progress, message, transient=False):
    """Send a progress line, if anyone is listening.

    `transient` marks a line that is worth showing only while
    nothing better is on screen — the byte counter, which is
    superseded a moment later. Milestones (what we asked, what it
    said, what failed) are not transient: each has to hold the line
    long enough to be read, which is the receiver's job.

    Callbacks arrive normalised to two arguments by `_accept`, so a
    caller that only wants the text can still pass `print`. Never
    let a
    reporting failure break a download: the callback belongs to the
    GUI, and a fetch that dies because a status bar misbehaved would
    be a poor trade."""
    if on_progress is None:
        return
    try:
        on_progress(message, transient)
    except Exception:
        pass


def _accept(on_progress):
    """Normalise a progress callback to `(message, transient)`.

    Callers that only care about the text — a script, a test — pass
    a one-argument callable, and should not have to learn about
    transience to do so. Decided once here rather than per message:
    calling with two arguments and retrying on TypeError would call
    a one-argument callback twice whenever it raised TypeError of
    its own."""
    if on_progress is None:
        return None
    try:
        params = inspect.signature(on_progress).parameters
        takes_two = len([
            p for p in params.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= 2
        if any(p.kind == p.VAR_POSITIONAL for p in params.values()):
            takes_two = True
    except (TypeError, ValueError):
        takes_two = False       # builtins (list.append) refuse inspection
    if takes_two:
        return on_progress
    return lambda message, transient=False: on_progress(message)


# How often a download may report its byte count. Fast enough to
# look live, slow enough that a 28 MB PDF does not post hundreds of
# updates into the GUI's main loop.
_PROGRESS_INTERVAL_S = 0.3


def _curl_download(url, target_path, timeout):
    """curl fallback for sites whose Cloudflare rejects urllib's TLS
    fingerprint. Same args as `download_pdf` minus the headers
    dance (curl handles UA via -A). Returns (ok, msg)."""
    try:
        out = subprocess.run(
            ["curl", "--silent", "--show-error", "--fail",
             "--location", "--max-time", str(timeout),
             "-A", _BROWSER_HEADERS["User-Agent"],
             "-H", "Accept: " + _BROWSER_HEADERS["Accept"],
             "-o", target_path, url],
            capture_output=True, text=True, timeout=timeout + 5)
    except FileNotFoundError:
        return False, "curl not installed"
    except subprocess.TimeoutExpired:
        return False, "curl timed out"
    if out.returncode != 0:
        msg = (out.stderr or "").strip().splitlines()
        return False, (msg[-1] if msg else "curl exit {}".format(out.returncode))
    return True, ""


def download_pdf(url, target_path, timeout=60, on_progress=None):
    """Download `url` to `target_path` (atomic .tmp + rename).
    Returns (ok, msg). On Cloudflare blocks (HTTP 403 with a
    Cloudflare server header, or HTTP 200 with an HTML body) retry
    via curl, which presents a different TLS ClientHello and is
    usually accepted. Sanity-checks the result is a real PDF
    (%PDF- magic + reasonable size).

    `on_progress` receives short human-readable lines: connecting,
    the reply, and bytes as they arrive. A 28 MB paper takes half a
    minute on a slow publisher, and a caller with no way to say so
    can only offer a frozen "Looking…"."""
    on_progress = _accept(on_progress)
    tmp = target_path + ".tmp"
    host = host_of(url)
    _report(on_progress, "Connecting to {}…".format(host))
    try:
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if (total or "").isdigit() else None
            _report(on_progress, "{} replied ({}) — downloading {}".format(
                host, getattr(resp, "status", 200) or 200,
                human_size(total) if total else "unknown size"))
            done = 0
            last = 0.0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last >= _PROGRESS_INTERVAL_S:
                        last = now
                        _report(on_progress,
                                "Downloading from {} — {}{}".format(
                                    host, human_size(done),
                                    " of " + human_size(total)
                                    if total else ""),
                                transient=True)
    except urllib.error.HTTPError as e:
        _silent_remove(tmp)
        if e.code == 403 and "cloudflare" in (
                (e.headers.get("server") or "").lower()
                if e.headers else ""):
            ok, curl_msg = _curl_download(url, tmp, timeout)
            if not ok:
                _silent_remove(tmp)
                return False, ("blocked by Cloudflare; curl fallback "
                               "also failed ({})".format(curl_msg))
        elif e.code == 403:
            return False, ("HTTP 403 Forbidden — publisher refused "
                           "the download")
        elif e.code == 404:
            return False, "HTTP 404 — PDF URL no longer valid"
        else:
            return False, "HTTP {} {}".format(e.code, e.reason or "")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        _silent_remove(tmp)
        return False, str(e)

    # Sanity-check the bytes we actually got.
    try:
        size = os.path.getsize(tmp)
        with open(tmp, "rb") as f:
            head = f.read(5)
    except OSError as e:
        return False, str(e)
    if size < 1024 or head != b"%PDF-":
        looks_like_html = (head[:5].lower().startswith(b"<htm")
                           or head[:5] == b"<!DOC"[:5])
        if looks_like_html:
            _silent_remove(tmp)
            ok, curl_msg = _curl_download(url, tmp, timeout)
            if ok:
                try:
                    size = os.path.getsize(tmp)
                    with open(tmp, "rb") as f:
                        head = f.read(5)
                except OSError as e:
                    return False, str(e)
                if size < 1024 or head != b"%PDF-":
                    _silent_remove(tmp)
                    return False, "fetched body is not a PDF (curl too)"
            else:
                return False, ("fetched body is not a PDF; curl "
                               "fallback failed ({})".format(curl_msg))
        else:
            _silent_remove(tmp)
            return False, "fetched body is not a PDF"

    try:
        os.replace(tmp, target_path)
    except OSError as e:
        _silent_remove(tmp)
        return False, str(e)
    return True, ""


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ---- URL discovery -------------------------------------------------

def _openalex_pdf_urls(doi, timeout=15):
    """OpenAlex Work → ordered list of PDF URLs (best_oa_location
    first, then other OA locations). Empty list if OpenAlex has
    nothing or the lookup fails."""
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.openalex.org/works/doi:" + qdoi
    if metrics.OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(metrics.OPENALEX_MAILTO)
    data = metrics._http_get_json(
        url,
        headers={"User-Agent": metrics.OPENALEX_UA,
                 "Accept": "application/json"},
        timeout=timeout)
    if not data:
        return []
    urls = []
    bol = data.get("best_oa_location") or {}
    if bol.get("pdf_url"):
        urls.append(bol["pdf_url"])
    for loc in (data.get("locations") or []):
        if not loc.get("is_oa"):
            continue
        u = loc.get("pdf_url")
        if u and u not in urls:
            urls.append(u)
    return urls


def _unpaywall_report(doi):
    """`(pdf_urls, message)` from Unpaywall.

    "Unpaywall: nothing" covered three different situations, and only
    one of them is a problem:

      - it did not answer, or has never heard of the DOI;
      - it answered and knows of no open-access copy at all;
      - it answered, the paper *is* open access, but every location
        it holds is a landing page with no direct PDF link.

    The third is the surprising one — Slice'N'Dice is hybrid OA with
    two locations and no downloadable file among them — and the one
    most likely to be read as a fault. `fetch_oa_locations` already
    drops locations without a `url_for_pdf`, but it keeps `is_oa` and
    `oa_status`, which is enough to tell the three apart."""
    try:
        unpw = metrics.fetch_oa_locations(doi)
    except Exception as e:
        return [], "Unpaywall: no answer ({})".format(e)
    if not unpw:
        return [], "Unpaywall: no answer, or no record of this DOI"
    urls = []
    for loc in unpw.get("locations") or []:
        u = loc.get("pdf_url")
        if u and u not in urls:
            urls.append(u)
    if urls:
        return urls, "Unpaywall: {}".format(_found(len(urls)))
    if unpw.get("is_oa"):
        return [], ("Unpaywall: open access ({}) but no direct PDF "
                    "link — landing pages only".format(
                        unpw.get("oa_status") or "status unknown"))
    return [], "Unpaywall: no open-access copy known"


def _unpaywall_pdf_urls(doi):
    """Just the URLs — see `_unpaywall_report` for the reasoning."""
    return _unpaywall_report(doi)[0]


def _europepmc_pdf_urls(doi, timeout=15):
    """EuropePMC search for `DOI:<doi>` → fullTextUrlList → PDF URLs.

    Preference order: the official EuropePMC PMC PDF first (the
    canonical Cloudflare-bypass for NIH/UKRI-funded papers), then
    any other publisher / repository PDF the response carries.
    Empty list if no EuropePMC record or no PDF URLs.

    Note: we also try the predictable `europepmc.org/articles/PMCxxx?pdf=render`
    URL when EuropePMC returns a PMCID but no explicit PDF link —
    that endpoint always serves the PDF for OA PMC papers."""
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search"
           "?query=DOI:" + urllib.parse.quote(doi, safe="")
           + "&resultType=core&format=json")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": metrics.EUROPEPMC_UA,
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError,
            OSError, ValueError, TimeoutError):
        return []
    results = ((data.get("resultList") or {}).get("result") or [])
    if not results:
        return []
    r0 = results[0]
    urls = []
    for u in (r0.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        if (u.get("documentStyle") or "").lower() == "pdf":
            href = (u.get("url") or "").strip()
            if href and href not in urls:
                urls.append(href)
    # Predictable PMC render-pdf endpoint as a safety net.
    pmcid = (r0.get("pmcid") or "").strip()
    if pmcid:
        href = "https://europepmc.org/articles/{}?pdf=render".format(pmcid)
        if href not in urls:
            urls.append(href)
    return urls


def unavailable_sources():
    """Names of the OA sources that cannot run as configured.

    Unpaywall requires a contact address — it is in their terms, not
    a politeness convention, and `metrics.fetch_oa_locations` returns
    None without one. That silence is the problem: a Get PDF that
    quietly consults two sources instead of three looks identical to
    one that found nothing, so the user has no way to learn that
    filling in a preference would have helped.

    Returned as names rather than a bool so the caller can say which
    source it is skipping, and so a second source with its own
    requirement has somewhere to go."""
    missing = []
    if not metrics.OPENALEX_MAILTO:
        missing.append("Unpaywall")
    return missing


def oa_pdf_urls_for_doi(doi, also_try_europepmc=True,
                        on_progress=None):
    """Ordered list of candidate OA PDF URLs, de-duplicated.

    Order: OpenAlex → Unpaywall → EuropePMC. The caller tries each
    until one downloads as a real PDF. EuropePMC is last so the
    canonical publisher / repository URLs get a chance first, but
    routinely saves the day for Cloudflare-blocked publishers."""
    on_progress = _accept(on_progress)
    _report(on_progress, "Asking OpenAlex about {}…".format(doi))
    urls = _openalex_pdf_urls(doi)
    _report(on_progress, "OpenAlex: {}".format(
        _found(len(urls))))

    skipped = unavailable_sources()
    if "Unpaywall" in skipped:
        _report(on_progress,
                "Skipping Unpaywall — it needs a contact email "
                "(Preferences → Online services)")
    else:
        _report(on_progress, "Asking Unpaywall…")
        found, message = _unpaywall_report(doi)
        fresh = 0
        for u in found:
            if u not in urls:
                urls.append(u)
                fresh += 1
        if found and not fresh:
            message += " — all already offered by OpenAlex"
        _report(on_progress, message)

    if also_try_europepmc:
        _report(on_progress, "Asking EuropePMC…")
        n = 0
        for u in _europepmc_pdf_urls(doi):
            if u not in urls:
                urls.append(u)
                n += 1
        _report(on_progress, "EuropePMC: {}".format(_found(n)))
    return urls


def _found(n):
    """"nothing" / "1 PDF" / "3 PDFs" — a progress line reads better
    than a bare count, and "0 PDFs" reads worse than "nothing"."""
    if not n:
        return "nothing"
    return "{} PDF{}".format(n, "" if n == 1 else "s")


def fetch_oa_pdf(doi, target_path, also_try_europepmc=True,
                 per_url_timeout=60, on_progress=None):
    """Try every OA URL for `doi` until one downloads as a real
    PDF. Returns `(ok, source_url, message)` — `source_url` is
    the URL that actually worked (None on failure), `message` is
    the last error explanation when all attempts failed.

    `on_progress` receives a line per step: which source is being
    asked, what it said, and how each download is going. The whole
    run can take half a minute, and every part of it is something
    the user would rather see than wait through."""
    on_progress = _accept(on_progress)
    urls = oa_pdf_urls_for_doi(doi, also_try_europepmc=also_try_europepmc,
                               on_progress=on_progress)
    if not urls:
        return (False, None,
                "no OA PDF URLs known to OpenAlex / Unpaywall / EuropePMC")
    last = ""
    for i, u in enumerate(urls, start=1):
        if len(urls) > 1:
            _report(on_progress, "Candidate {} of {}: {}".format(
                i, len(urls), host_of(u)))
        ok, msg = download_pdf(u, target_path, timeout=per_url_timeout,
                               on_progress=on_progress)
        if ok:
            _report(on_progress, "Got the PDF from {}".format(host_of(u)))
            return True, u, ""
        last = msg
        _report(on_progress, "{} failed: {}{}".format(
            host_of(u), msg, " — trying the next candidate"
            if i < len(urls) else ""))
    return False, None, last or "all PDF candidates failed"
