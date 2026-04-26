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

from .identity import maintainer_email

CROSSREF_USER_AGENT = os.environ.get(
    "PDFORG_CROSSREF_UA",
    "pdforg/0.1 (mailto:{})".format(maintainer_email()))

try:
    from pypdf import PdfReader
    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False

# pdfx is optional; when present, it gives us much richer XMP parsing
# than pypdf's /Info dict. We use it as a Python library (no
# subprocess) — pdf.summary returns the same shape as `pdfx -j` did.
try:
    import pdfx as _pdfx
    HAVE_PDFX = True
except ImportError:
    _pdfx = None
    HAVE_PDFX = False


def _have_pdfx():
    return HAVE_PDFX


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")

# pypdf info-dict keys we trust to hold the *article* DOI directly,
# tried in priority order. Wiley's WPS pipeline writes both
# /WPS-ARTICLEDOI (the article) and /WPS-JOURNALDOI (a journal-level
# prefix that's a strict prefix of the article DOI), so picking by
# regex order alone yields the truncated journal value.
_ARTICLE_DOI_KEYS = ("/WPS-ARTICLEDOI", "/prism:doi", "/doi")
_JOURNAL_DOI_KEYS = ("/WPS-JOURNALDOI",)

# A page-1 line that signals "this PDF is the supporting information
# document, not the article". ACS, RSC, Nature, Wiley and most others
# all start their SI cover with one of these phrases.
_SI_HEADER_RE = re.compile(
    r"^\s*(?:supporting|supplementary|electronic\s+supplementary)\s+"
    r"(?:information|material|materials)\b",
    re.IGNORECASE)

# ACS SI filename like "ci4c02293_si_001.pdf". The journal-prefix lookup
# below maps "ci" to "jcim" so we can synthesise the parent DOI without
# a network round-trip.
_ACS_SI_FILENAME_RE = re.compile(
    r"^(?P<prefix>[a-z]+)(?P<id>\d+[a-z]+\d+)_si_\d+\.pdf$",
    re.IGNORECASE)

# Most modern ACS DOIs use the "10.1021/acs.<journal>.<id>" form.
_ACS_PREFIX_TO_JOURNAL = {
    "ac": "analchem",      # Analytical Chemistry
    "bc": "bioconjchem",   # Bioconjugate Chem
    "bi": "biochem",       # Biochemistry
    "bm": "biomac",        # Biomacromolecules
    "ci": "jcim",          # J. Chem. Inf. Model.
    "ct": "jctc",          # J. Chem. Theory Comput.
    "es": "est",           # Environ. Sci. Technol.
    "ic": "inorgchem",     # Inorg. Chem.
    "jp": "jpcb",          # JPCB (also JPCA/JPCC; coarse but usually right)
    "jo": "joc",           # J. Org. Chem.
    "la": "langmuir",      # Langmuir
    "ma": "macromol",      # Macromolecules
    "mp": "molpharm",      # Mol. Pharm.
    "nl": "nanolett",      # Nano Lett.
    "ol": "orglett",       # Org. Lett.
}

# Legacy ACS journals that don't use the "acs." infix.
_ACS_PREFIX_NO_INFIX = {
    "ja": "jacs",          # JACS — DOI is 10.1021/jacs.<id>
}


def _is_supplementary(pdf_path, page1_text):
    """True if this PDF is a supporting-information document (not the
    article itself). Two signals: page-1 cover header, or an SI-suffixed
    filename in a known publisher pattern."""
    if page1_text:
        for line in page1_text.splitlines():
            s = line.strip()
            if not s:
                continue
            return bool(_SI_HEADER_RE.match(s))
    name = os.path.basename(pdf_path or "").lower()
    if _ACS_SI_FILENAME_RE.match(name):
        return True
    return False


def _parent_doi_from_si_filename(pdf_path):
    """Synthesise the parent paper's DOI from an ACS SI filename like
    ci4c02293_si_001.pdf. Returns None for non-ACS or unmapped prefixes."""
    name = os.path.basename(pdf_path or "").lower()
    m = _ACS_SI_FILENAME_RE.match(name)
    if not m:
        return None
    prefix = m.group("prefix")
    article = m.group("id")
    if prefix in _ACS_PREFIX_NO_INFIX:
        return "10.1021/{}.{}".format(_ACS_PREFIX_NO_INFIX[prefix], article)
    if prefix in _ACS_PREFIX_TO_JOURNAL:
        return "10.1021/acs.{}.{}".format(
            _ACS_PREFIX_TO_JOURNAL[prefix], article)
    return None


# Filename → DOI patterns for the *article* (not SI). These cover
# common publisher download-naming conventions where the DOI is
# trivially recoverable from the filename alone, so we can avoid a
# pdftotext page-scan or an OpenAlex round-trip.
_PNAS_FILENAME_RE = re.compile(
    r"^pnas[\-.](?P<id>\d{7,})\b",
    re.IGNORECASE)
# bioRxiv / medRxiv naming: 2024.05.24.595765(.full)?.pdf
_BIORXIV_FILENAME_RE = re.compile(
    r"^(?P<id>\d{4}\.\d{2}\.\d{2}\.\d{6,})\b",
    re.IGNORECASE)
# Science Advances PDFs are downloaded as "sciadv.<id>.pdf" and the
# DOI is 10.1126/sciadv.<id> — but Science Advances doesn't print
# the DOI in the article body, so this is the *only* place to get it
# without an OpenAlex search.
_SCIADV_FILENAME_RE = re.compile(
    r"^sciadv\.(?P<id>[a-z0-9]+)\.pdf$",
    re.IGNORECASE)
# Filenames that just *are* the DOI with the slash safely encoded as
# an underscore: "10.1186_s13321-024-00821-4.pdf".
_DOI_AS_FILENAME_RE = re.compile(
    r"^(?P<prefix>10\.\d{4,9})_(?P<rest>[A-Za-z0-9][\w.\-]*)\.pdf$",
    re.IGNORECASE)


def _doi_from_filename(pdf_path):
    """Recover the article DOI from publisher-conventional filenames.

    Free and instant compared to `_scan_doi_in_pages` (which spawns
    pdftotext) or `find_doi` (which hits OpenAlex). Returns None for
    filenames that don't match a known pattern."""
    name = os.path.basename(pdf_path or "")
    m = _PNAS_FILENAME_RE.match(name)
    if m:
        return "10.1073/pnas.{}".format(m.group("id"))
    m = _BIORXIV_FILENAME_RE.match(name)
    if m:
        return "10.1101/{}".format(m.group("id"))
    m = _SCIADV_FILENAME_RE.match(name)
    if m:
        return "10.1126/sciadv.{}".format(m.group("id"))
    m = _DOI_AS_FILENAME_RE.match(name)
    if m:
        return "{}/{}".format(m.group("prefix"), m.group("rest"))
    return None


def _parse_si_parent_title_authors(text):
    """Lift the parent paper's title and author list from the SI cover.

    The cover layout is consistently:

        SUPPORTING INFORMATION
        [for]
        <title line 1>
        [<title line 2> ...]
        <author line(s) — first one carrying digits/asterisks>

    Returns (title, authors) where either may be None / []."""
    if not text:
        return None, []
    lines = [l.rstrip() for l in text.splitlines()]
    i = 0
    while i < len(lines) and not _SI_HEADER_RE.match(lines[i].strip()):
        i += 1
    if i >= len(lines):
        return None, []
    i += 1  # skip the SUPPORTING INFORMATION line
    while i < len(lines) and (
            not lines[i].strip() or lines[i].strip().lower() == "for"):
        i += 1
    title_lines = []
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            break
        # Author line markers: digit footnote indices, footnote symbols,
        # or " and " linking the last two names.
        if re.search(r"\d", s) or any(c in s for c in "*†‡§¶") \
                or re.search(r"\s+and\s+", s, re.I):
            break
        title_lines.append(s)
        i += 1
    author_blob = []
    while i < len(lines) and lines[i].strip():
        author_blob.append(lines[i].strip())
        i += 1
    title = " ".join(title_lines).strip(" .,;:") or None
    authors = []
    if author_blob:
        joined = " ".join(author_blob)
        # Strip footnote markers and normalise " and " to a comma.
        joined = re.sub(r"[\d*†‡§¶]+", "", joined)
        joined = re.sub(r"\s+and\s+", ", ", joined, flags=re.I)
        authors = [a.strip(" ,.") for a in joined.split(",") if a.strip()]
        authors = [a for a in authors if len(a) >= 3]
    return title, authors

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
    # Publisher placeholder titles — same string for every paper from
    # the same imprint. Match leniently because em-dash / en-dash /
    # hyphen variants all show up.
    if low.startswith("science journals"):  # AAAS / Science magazine
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


def _run_pdfx(pdf_path):
    """Open `pdf_path` via the pdfx library and return its summary
    dict (same shape `pdfx -j <pdf>` produced on stdout: keys
    `source`, `metadata`, `references`). Returns None on any error."""
    if not HAVE_PDFX:
        return None
    try:
        pdf = _pdfx.PDFx(pdf_path)
    except Exception:
        return None
    try:
        return pdf.summary
    except Exception:
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
    data = _run_pdfx(pdf_path)
    if not data:
        return None
    md = data.get("metadata", {}) or {}
    out = {"title": None, "authors": [], "year": None,
           "doi": None, "journal": None, "raw": md}

    dc = md.get("dc", {}) or {}
    # Match any PRISM XMP namespace version. Publishers ship 2.0,
    # 2.1, 3.0 and even bare "prism" — Science Advances uses 2.1,
    # which a hardcoded "2.0 or 3.0" check would miss entirely.
    prism = md.get("prism") or {}
    if not prism:
        for key, val in md.items():
            if (isinstance(key, str)
                    and key.startswith(
                        "http://prismstandard.org/namespaces/basic/")
                    and isinstance(val, dict)):
                prism = val
                break
    crossmark = md.get("crossmark") or {}
    # The in-XMP "pdfx" namespace (different from the pdfx Python
    # library): journals like Science Advances stash a duplicate of
    # the article DOI here.
    pdfx_ns = md.get("pdfx") or {}

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

    # DOI hunt: try every known XMP slot. Different publishers stamp
    # the same DOI into different namespaces; some only one of them.
    # `dc.identifier` is typically prefixed with "doi:" — _DOI_RE
    # below picks the bare DOI back out.
    out["doi"] = (_first_str(md.get("doi"))
                  or _first_str(prism.get("doi"))
                  or _first_str(prism.get("identifier"))
                  or _first_str(crossmark.get("DOI"))
                  or _first_str(crossmark.get("doi"))
                  or _first_str(pdfx_ns.get("doi"))
                  or _first_str(dc.get("identifier")))
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


def _scan_doi_in_pages(pdf_path, max_pages=4):
    """Search for a DOI across the first `max_pages` of the PDF.
    Used as a fallback when the page-1 scrape doesn't find one — some
    journals (Science, PNAS, ...) put the DOI in a footer or near the
    references rather than on page 1. Returns the first DOI found, or None."""
    if not shutil.which("pdftotext"):
        return None
    try:
        proc = subprocess.run(
            ["pdftotext", "-f", "1", "-l", str(max_pages), pdf_path, "-"],
            capture_output=True, text=True, timeout=30, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _scrape_doi(proc.stdout or "")


# DOI prefixes used by data repositories — the paper's *publisher*
# DOI is almost always more useful, so we prefer non-data DOIs when
# both kinds appear in the body text. (Common in Methods / Data
# Availability sections that link to deposited data.)
_DATA_DOI_PREFIXES = (
    "10.5281/zenodo.",
    "10.6084/",          # figshare
    "10.5061/dryad",     # Dryad
    "10.17605/osf",      # OSF
    "10.7910/dvn",       # Harvard Dataverse
)


def _is_data_doi(doi):
    if not doi:
        return False
    low = doi.lower()
    return any(low.startswith(p) for p in _DATA_DOI_PREFIXES)


def _doi_from_info(raw):
    """Pick the article DOI from a pypdf info dictionary.

    Some publishers embed multiple DOI-shaped strings: Wiley's PDFs,
    for example, carry both /WPS-JOURNALDOI (a journal-level prefix
    like '10.1107/S20597983') and /WPS-ARTICLEDOI (the actual
    article DOI '10.1107/S205979831700969X'). A naive regex scan
    over the joined blob picks whichever the dict iterates first
    and silently truncates the result.

    Strategy: look at known per-article keys in priority order; if
    none hit, scan the remaining values (excluding known
    journal-level keys), drop any DOI that is a strict prefix of
    another, then prefer publisher DOIs over data-repo DOIs the
    same way `_scrape_doi` does."""
    for k in _ARTICLE_DOI_KEYS:
        v = raw.get(k)
        if v:
            m = _DOI_RE.search(str(v))
            if m:
                return m.group(0).rstrip(".,;")
    blob = " ".join(str(v) for k, v in raw.items()
                    if k not in _JOURNAL_DOI_KEYS)
    matches = _DOI_RE.findall(blob)
    if not matches:
        return None
    seen = []
    for raw_match in matches:
        d = raw_match.rstrip(".,;")
        if d and d not in seen:
            seen.append(d)
    seen = [d for d in seen
            if not any(other != d and other.startswith(d) for other in seen)]
    if not seen:
        return None
    publisher = [d for d in seen if not _is_data_doi(d)]
    return publisher[0] if publisher else seen[0]


def _scrape_doi(text):
    """Find a DOI in arbitrary text. Returns the most likely publisher
    DOI, or None. When both a publisher DOI and one or more data-repo
    DOIs appear (Zenodo / figshare / Dryad / OSF / Dataverse), prefer
    the publisher DOI."""
    if not text:
        return None
    matches = re.findall(
        r"(?:doi(?:\.org)?[:/]\s*)?(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
        text, re.IGNORECASE)
    if not matches:
        return None
    seen = []
    for raw in matches:
        d = raw.rstrip(".,;)]\"'").split()[0]
        if d and d not in seen:
            seen.append(d)
    if not seen:
        return None
    publisher = [d for d in seen if not _is_data_doi(d)]
    return publisher[0] if publisher else seen[0]


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
    # Reject all-caps short lines: running headers ("RES EARCH",
    # "ELECTRON MICROSCOPY") and section headings. Real titles
    # have mixed case.
    letters = [c for c in s if c.isalpha()]
    if letters and len(s) < 40:
        upper_frac = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_frac >= 0.85:
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
    CrossRef data. Also detects SI documents and re-routes them through
    the parent paper's DOI."""
    text = _first_page_text(pdf_path)
    is_si = _is_supplementary(pdf_path, text)
    if is_si:
        result["is_supplementary"] = True
        # Page-1 / pdfx scrapes will have lifted the SI cover's title
        # and authors. They describe the parent paper but routinely get
        # truncated mid-sentence (the comma after "Persistence,"
        # becomes the title boundary; everything after becomes
        # "authors"). Discard them and let the parent-DOI lookup
        # repopulate from the canonical record.
        si_title, si_authors = _parse_si_parent_title_authors(text)
        result["title"] = None
        result["authors"] = []
        if not result.get("doi"):
            doi = _parent_doi_from_si_filename(pdf_path)
            if not doi and si_title:
                # Lazy import: pdforg.metrics pulls in the OpenAlex /
                # CrossRef HTTP machinery that pure-PDF callers don't
                # need.
                from . import metrics as _metrics
                doi = _metrics.find_doi(si_title, author_names=si_authors)
            if doi:
                result["doi"] = doi
    elif not result.get("doi"):
        doi = _scrape_doi(text)
        if not doi:
            # Filename-based inference: free and instant for the
            # common publisher conventions (PNAS / bioRxiv /
            # DOI-as-filename). Older PNAS PDFs in particular have
            # the DOI nowhere in their text but encode it in the
            # filename ("pnas-0502225102.pdf").
            doi = _doi_from_filename(pdf_path)
        if not doi:
            # Some journals (e.g. Science) print the DOI on the references
            # page rather than page 1. Cast a wider net before giving up.
            doi = _scan_doi_in_pages(pdf_path, max_pages=4)
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

    # Last-resort: scrape page 1 if we still have no title — but skip
    # for SI documents because the cover page describes the parent
    # paper and the heuristic scraper routinely clips it badly.
    if not is_si and (
            not result.get("title") or not result.get("authors")):
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

    out["doi"] = _doi_from_info(raw)

    cd = raw.get("/CreationDate") or raw.get("CreationDate") or ""
    out["year"] = _parse_pdf_date(cd)

    return _enrich(out, pdf_path)
