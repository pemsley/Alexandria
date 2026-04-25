"""Best-effort metadata extraction from a PDF.

Tries pdfx first (rich XMP parsing), then pypdf, then a stub. The caller
can later overlay better metadata from CrossRef / arXiv lookups.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request

def _resolve_pdfx_bin():
    """Locate the pdfx executable. Order: $PDFORG_PDFX env var (if set),
    then $PATH lookup. Returns None if not found, in which case the
    extractor falls back to pypdf and the page-1 scrape."""
    override = os.environ.get("PDFORG_PDFX")
    if override:
        return override
    return shutil.which("pdfx")


PDFX_BIN = _resolve_pdfx_bin()

from .identity import maintainer_email

CROSSREF_USER_AGENT = os.environ.get(
    "PDFORG_CROSSREF_UA",
    "pdforg/0.1 (mailto:{})".format(maintainer_email()))

try:
    from pypdf import PdfReader
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False


def _have_pdfx():
    return bool(PDFX_BIN) and os.path.isfile(PDFX_BIN) and os.access(PDFX_BIN, os.X_OK)


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")

# Known placeholder/garbage values that publishers leave in /Info.
_GARBAGE_TITLES = {
    "no job name",
    "untitled",
    "untitled.dvi",
    "untitled document",
    "microsoft word",
    "(microsoft word",
    "title",
    "doc1",
}


def _is_garbage_title(s):
    if not s:
        return True
    s_strip = s.strip()
    low = s_strip.lower()
    if len(low) < 4:
        return True
    if low in _GARBAGE_TITLES:
        return True
    if low.startswith("microsoft word -"):
        return True
    if low.endswith(".dvi") or low.endswith(".tex") or low.endswith(".docx"):
        return True
    # Typesetting placeholders / template tokens that the workflow
    # forgot to substitute (e.g. "TX_1~ABS:AT/TX_2~ABS~AT").
    if "~" in s_strip and " " not in s_strip:
        return True
    if re.fullmatch(r"[A-Z0-9_~:/.\-]{8,}", s_strip):
        return True
    # Page-range fragments left in /Title (e.g. "bbq089online 689..701",
    # "ar1-9", "p123-145"). Real titles essentially never contain
    # consecutive dots or "ddd..ddd" forms.
    if ".." in s_strip:
        return True
    if re.search(r"\b\d{2,4}\s*[\-–.]+\s*\d{2,4}\b", s_strip):
        # Combined with no spaces / very short, this is publisher-junk.
        if len(s_strip.split()) <= 3:
            return True
    return False


def _sane_year(y):
    """Return y if it's a plausible publication year, else None."""
    try:
        n = int(y)
    except (TypeError, ValueError):
        return None
    # Don't trust anything earlier than the first scientific journals
    # (~1665) or much past today.
    from datetime import date as _date
    if 1900 <= n <= _date.today().year + 1:
        return n
    return None


def _parse_pdf_date(s):
    """Parse a PDF /CreationDate-style string and return a year, or None.
    Accepts D:YYYYMMDDHHmmSS... and validates month/day."""
    if not s:
        return None
    m = re.search(r"D:(\d{4})(\d{2})(\d{2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return _sane_year(y)
    return None


def _split_authors(s):
    if not s:
        return []
    # Common separators: " and ", "; ", "/", ", "
    for sep in [" and ", ";", "/"]:
        if sep in s:
            return [a.strip() for a in s.split(sep) if a.strip()]
    parts = [a.strip() for a in s.split(",")]
    # Heuristic: if we see lots of single-token splits, it's probably "Last, First, Last, First..."
    if len(parts) >= 4 and all(len(p.split()) <= 2 for p in parts):
        return parts
    return [s.strip()] if s.strip() else []


def _run_pdfx_json(pdf_path):
    """Shell out to pdfx -j and return the parsed dict, or None."""
    try:
        proc = subprocess.run(
            [PDFX_BIN, "-j", pdf_path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError):
        return None


def _first_str(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list) and v:
        return _first_str(v[0])
    if isinstance(v, dict):
        # XMP rdf:Alt / rdf:Seq style: take any string value
        for vv in v.values():
            s = _first_str(vv)
            if s:
                return s
    return None


def _extract_from_pdfx(pdf_path):
    data = _run_pdfx_json(pdf_path)
    if not data:
        return None
    md = data.get("metadata", {}) or {}
    out = {"title": None, "authors": [], "year": None,
           "doi": None, "journal": None, "raw": md}

    dc = md.get("dc", {}) or {}
    prism = (md.get("prism")
             or md.get("http://prismstandard.org/namespaces/basic/3.0/")
             or md.get("http://prismstandard.org/namespaces/basic/2.0/")
             or {})

    title_raw = (_first_str(dc.get("title"))
                 or _first_str(md.get("Title"))
                 or _first_str(prism.get("title")))
    out["title"] = None if _is_garbage_title(title_raw) else title_raw

    creators = dc.get("creator")
    if isinstance(creators, list):
        out["authors"] = [c for c in (str(x).strip() for x in creators) if c]
    elif isinstance(creators, str):
        out["authors"] = _split_authors(creators)
    elif md.get("Author"):
        out["authors"] = _split_authors(_first_str(md.get("Author")))

    out["doi"] = (_first_str(md.get("doi"))
                  or _first_str(prism.get("doi"))
                  or _first_str(prism.get("identifier")))
    if out["doi"]:
        m = _DOI_RE.search(out["doi"])
        if m:
            out["doi"] = m.group(0).rstrip(".,;")

    out["journal"] = (_first_str(prism.get("publicationName"))
                      or _first_str(prism.get("publication"))
                      or _first_str(dc.get("source")))

    # ACS (and some others) stash structured info in dc:subject,
    # e.g.  "article doi: 10.1021/...."  and
    # "Article metadata: <Journal>_<vol>_<issue>_<doi>_<startpage>_<endpage>"
    subjects = dc.get("subject") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    for s in subjects:
        s_str = _first_str(s)
        if not s_str:
            continue
        if not out["doi"]:
            m = _DOI_RE.search(s_str)
            if m:
                out["doi"] = m.group(0).rstrip(".,;)")
        if not out["journal"]:
            m = re.match(r"\s*Article\s+metadata\s*:\s*([^_]+)_",
                         s_str, re.IGNORECASE)
            if m:
                out["journal"] = m.group(1).strip()

    # Try typed XMP dates first (publicationDate / coverDate / dc.date).
    # Only fall back to the raw /CreationDate after strict parsing.
    for src in (prism.get("publicationDate"),
                prism.get("coverDate"),
                dc.get("date")):
        s = _first_str(src)
        if s:
            m = _YEAR_RE.search(s)
            if m:
                y = _sane_year(m.group(0))
                if y:
                    out["year"] = y
                    break
    if out["year"] is None:
        out["year"] = _parse_pdf_date(_first_str(md.get("CreationDate")))

    return out


_COVER_PAGE_MARKERS = (
    "see discussions, stats, and author profiles",
    "researchgate.net/publication",
    "this article appeared in a journal published by elsevier",
    "the attached copy is furnished to the author",
)


def _looks_like_cover_page(text):
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in _COVER_PAGE_MARKERS)


def _first_page_text(pdf_path):
    """Render the first 'real' page to plain text via pdftotext.

    If page 1 looks like a publisher / aggregator cover sheet
    (ResearchGate, Elsevier reprint, etc.) and there's a page 2,
    return page 2's text instead. Returns '' on failure."""
    if not shutil.which("pdftotext"):
        return ""
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-f", "1", "-l", "2", pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
            errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    text = proc.stdout or ""
    # pdftotext separates pages with \x0c (form-feed).
    pages = [p for p in text.split("\x0c") if p.strip()]
    if not pages:
        return ""
    if len(pages) >= 2 and _looks_like_cover_page(pages[0]):
        return pages[1]
    return pages[0]


def _scrape_doi(text):
    """Find a DOI in arbitrary text. Tries doi.org URLs first, then bare DOIs."""
    if not text:
        return None
    m = re.search(r"(?:doi(?:\.org)?[:/]\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
                  text, re.IGNORECASE)
    if not m:
        return None
    doi = m.group(1).rstrip(".,;)]\"'")
    # Sometimes the regex grabs trailing junk like "ThisarticleisaUSGov..."
    # — strip at the first whitespace anyway.
    doi = doi.split()[0]
    return doi


def _crossref_lookup(doi):
    """Fetch metadata for a DOI from CrossRef. Returns dict or None."""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": CROSSREF_USER_AGENT,
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    msg = data.get("message", {}) or {}
    out = {"title": None, "authors": [], "journal": None, "year": None}
    titles = msg.get("title")
    if isinstance(titles, list) and titles:
        out["title"] = str(titles[0]).strip() or None
    authors = msg.get("author") or []
    names = []
    for a in authors:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        full = (given + " " + family).strip()
        if full:
            names.append(full)
    out["authors"] = names
    cont = msg.get("container-title")
    if isinstance(cont, list) and cont:
        out["journal"] = str(cont[0]).strip() or None
    issued = msg.get("issued", {}).get("date-parts") or []
    if issued and issued[0]:
        try:
            out["year"] = int(issued[0][0])
        except (ValueError, TypeError, IndexError):
            pass
    return out


_JUNK_LINE_RE = re.compile(
    r"^\s*("
    r"arxiv\b"
    r"|doi[:/]"
    r"|preprint\b"
    r"|page\s+\d+"
    r"|\d+\s*$"                 # bare page number
    r"|https?://"
    r"|copyright\b|©"
    r"|received\b|accepted\b|published\b"
    r"|vol\.|volume\b|chapter\b"
    r")", re.IGNORECASE)


def _looks_like_title(line):
    s = line.strip()
    if len(s) < 8 or len(s) > 250:
        return False
    if _JUNK_LINE_RE.search(s):
        return False
    # Reject lines that are mostly digits or symbols.
    alpha = sum(1 for c in s if c.isalpha())
    if alpha < max(8, int(0.5 * len(s))):
        return False
    return True


def _split_author_line(line):
    s = line.strip()
    if not s:
        return []
    # "Marcin Novotni      Reinhard Klein"  -> split on 2+ spaces
    if re.search(r"\s{2,}", s):
        parts = re.split(r"\s{2,}", s)
        return [p.strip(" ,;.") for p in parts if p.strip(" ,;.")]
    # "Author A, Author B and Author C"
    s2 = re.sub(r"\s+and\s+", ", ", s, flags=re.IGNORECASE)
    return [p.strip(" ,;.") for p in s2.split(",") if p.strip(" ,;.")]


def _looks_like_authors(line):
    parts = _split_author_line(line)
    if not parts:
        return False
    # Each part should look name-shaped: 2+ capitalised words, no long sentence.
    for p in parts:
        if len(p) > 80:
            return False
        if not re.match(r"^[A-ZÀ-Ý]", p):
            return False
        if any(c in p for c in ":/{}<>="):
            return False
    return 1 <= len(parts) <= 12


def _scrape_first_page(pdf_path):
    """Heuristic title/authors from page-1 text. Returns (title, authors)."""
    text = _first_page_text(pdf_path)
    if not text:
        return None, []
    lines = [ln.strip() for ln in text.splitlines()]
    title = None
    title_idx = None
    for i, ln in enumerate(lines):
        if _looks_like_title(ln):
            title = ln
            title_idx = i
            # Stitch next line if it looks like a title continuation
            # (short, capitalised, not author-shaped).
            for j in range(i + 1, min(i + 3, len(lines))):
                cont = lines[j]
                if not cont:
                    break
                if _looks_like_authors(cont) or _JUNK_LINE_RE.search(cont):
                    break
                if len(cont) > 100:
                    break
                if cont.endswith(":") or cont[:1].islower():
                    title += " " + cont
                    title_idx = j
                else:
                    break
            break
    authors = []
    if title_idx is not None:
        for k in range(title_idx + 1, min(title_idx + 6, len(lines))):
            cand = lines[k]
            if not cand:
                continue
            if _looks_like_authors(cand):
                authors = _split_author_line(cand)
                break
            if "abstract" in cand.lower():
                break
    return title, authors


def _enrich(result, pdf_path):
    """If metadata is incomplete, scrape page 1 for a DOI and overlay
    CrossRef data."""
    if not result.get("doi"):
        text = _first_page_text(pdf_path)
        doi = _scrape_doi(text)
        if doi:
            result["doi"] = doi

    cr = _crossref_lookup(result["doi"]) if result.get("doi") else None
    if cr:
        # Title and authors: only fill if missing (PDF metadata is
        # usually fine, sometimes nicer than CrossRef capitalisation).
        if not result.get("title") and cr["title"]:
            result["title"] = cr["title"]
        if not result.get("authors") and cr["authors"]:
            result["authors"] = cr["authors"]
        # Year and journal: prefer CrossRef. PDF /CreationDate is often
        # a re-stamp date (wrong year), and embedded journal names are
        # frequently abbreviations (e.g. "Biophysj").
        if cr["year"]:
            result["year"] = cr["year"]
        if cr["journal"]:
            result["journal"] = cr["journal"]

    # Last-resort: scrape page 1 if we still have no title.
    if not result.get("title") or not result.get("authors"):
        t, a = _scrape_first_page(pdf_path)
        if t and not result.get("title"):
            result["title"] = t
        if a and not result.get("authors"):
            result["authors"] = a
    return result


def extract_from_pdf(pdf_path):
    """Return a dict with keys: title, authors, year, doi, journal, raw.
    Any field may be None / empty. Never raises on a malformed PDF."""
    out = {"title": None, "authors": [], "year": None,
           "doi": None, "journal": None, "raw": {}}

    if _have_pdfx():
        result = _extract_from_pdfx(pdf_path)
        if result is not None:
            return _enrich(result, pdf_path)

    if not HAVE_PYPDF:
        return _enrich(out, pdf_path)
    try:
        reader = PdfReader(pdf_path)
        info = reader.metadata or {}
    except Exception:
        return out

    raw = {}
    for k, v in (info or {}).items():
        try:
            raw[str(k)] = str(v)
        except Exception:
            pass
    out["raw"] = raw

    title = raw.get("/Title") or raw.get("Title")
    if title and not _is_garbage_title(title):
        out["title"] = title.strip() or None

    author = raw.get("/Author") or raw.get("Author")
    out["authors"] = _split_authors(author) if author else []

    blob = " ".join(str(v) for v in raw.values())
    m = _DOI_RE.search(blob)
    if m:
        out["doi"] = m.group(0).rstrip(".,;")

    cd = raw.get("/CreationDate") or raw.get("CreationDate") or ""
    out["year"] = _parse_pdf_date(cd)

    return _enrich(out, pdf_path)
