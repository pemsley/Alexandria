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

import json
import os
import shutil
import subprocess
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


def download_pdf(url, target_path, timeout=60):
    """Download `url` to `target_path` (atomic .tmp + rename).
    Returns (ok, msg). On Cloudflare blocks (HTTP 403 with a
    Cloudflare server header, or HTTP 200 with an HTML body) retry
    via curl, which presents a different TLS ClientHello and is
    usually accepted. Sanity-checks the result is a real PDF
    (%PDF- magic + reasonable size)."""
    tmp = target_path + ".tmp"
    try:
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f, 1 << 16)
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


def _unpaywall_pdf_urls(doi):
    """Unpaywall via `metrics.fetch_oa_locations`. Empty list on
    failure."""
    try:
        unpw = metrics.fetch_oa_locations(doi)
    except Exception:
        return []
    if not unpw:
        return []
    urls = []
    for loc in unpw.get("locations") or []:
        u = loc.get("pdf_url")
        if u and u not in urls:
            urls.append(u)
    return urls


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


def oa_pdf_urls_for_doi(doi, also_try_europepmc=True):
    """Ordered list of candidate OA PDF URLs, de-duplicated.

    Order: OpenAlex → Unpaywall → EuropePMC. The caller tries each
    until one downloads as a real PDF. EuropePMC is last so the
    canonical publisher / repository URLs get a chance first, but
    routinely saves the day for Cloudflare-blocked publishers."""
    urls = _openalex_pdf_urls(doi)
    for u in _unpaywall_pdf_urls(doi):
        if u not in urls:
            urls.append(u)
    if also_try_europepmc:
        for u in _europepmc_pdf_urls(doi):
            if u not in urls:
                urls.append(u)
    return urls


def fetch_oa_pdf(doi, target_path, also_try_europepmc=True,
                 per_url_timeout=60):
    """Try every OA URL for `doi` until one downloads as a real
    PDF. Returns `(ok, source_url, message)` — `source_url` is
    the URL that actually worked (None on failure), `message` is
    the last error explanation when all attempts failed."""
    urls = oa_pdf_urls_for_doi(doi, also_try_europepmc=also_try_europepmc)
    if not urls:
        return False, None, "no OA PDF URLs known to OpenAlex / Unpaywall / EuropePMC"
    last = ""
    for u in urls:
        ok, msg = download_pdf(u, target_path, timeout=per_url_timeout)
        if ok:
            return True, u, ""
        last = msg
    return False, None, last or "all PDF candidates failed"
