"""Citation-count lookup. OpenAlex first, CrossRef fallback.

Network calls are timeout-bounded and never raise — they return (None, None)
on any failure so the caller can decide whether to retry later.
"""

import json
import os
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


# Drop OpenAlex concepts below this confidence score. 0.4 keeps the
# specific topics (e.g. "Glycoprotein") and trims the generic ones
# ("Chemistry", "Biology") that OpenAlex sprays on most papers.
KEYWORD_SCORE_THRESHOLD = 0.4
KEYWORD_LIMIT = 8


def fetch_metrics(doi):
    """Return (count_int, source_str, keywords_list).
    Any field may be None/[]."""
    if not doi:
        return None, None, []
    n, kw = _openalex_metrics(doi)
    if n is not None:
        return n, "openalex", kw
    n = _crossref_count(doi)
    if n is not None:
        return n, "crossref", []
    return None, None, []


def fetch_citation_count(doi):
    """Backward-compatible wrapper returning just (count, source)."""
    n, src, _ = fetch_metrics(doi)
    return n, src


def _openalex_metrics(doi):
    """Return (cited_by_count, keywords) or (None, [])."""
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.openalex.org/works/doi:" + qdoi
    if OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(OPENALEX_MAILTO)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": OPENALEX_UA,
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None, []
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
    return n, kw


def _crossref_count(doi):
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.crossref.org/works/" + qdoi
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": CROSSREF_UA,
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    msg = data.get("message", {}) or {}
    n = msg.get("is-referenced-by-count")
    return int(n) if isinstance(n, int) else None
