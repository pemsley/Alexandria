"""Extract a paper's bibliography from its PDF using poppler-glib.

v1 scope: numbered references only ([N] or N. style). Returns a list
of {n, text, doi} dicts; the viewer can hit-test [N] markers in the
body and look up the matching entry. Author-year styles ("Smith,
2020") and reference ranges ("[1,2,3]") are deliberately out of
scope here — they're a much messier problem and best layered on
top once the numbered case is solid.
"""

import re

import gi
gi.require_version("Poppler", "0.18")
from gi.repository import Gio, Poppler


# Headers we accept as the start of the bibliography, case-insensitive.
# Each is matched as a stand-alone line (after stripping); section
# numbers like "5. References" are tolerated.
_BIB_HEADERS = (
    "references",
    "bibliography",
    "literature cited",
    "works cited",
    "reference list",
)
_HEADER_RE = re.compile(
    r"^\s*(?:\d+[\.\)]?\s+)?(" + "|".join(_BIB_HEADERS) + r")\s*$",
    re.IGNORECASE)

# Sections that legitimately follow the bibliography — stop parsing
# when we see one (so we don't slurp supplementary material).
_END_HEADERS = (
    "appendix",
    "supplementary",
    "supporting information",
    "acknowledgments",
    "acknowledgements",
)
_END_HEADER_RE = re.compile(
    r"^\s*(?:\d+[\.\)]?\s+)?(" + "|".join(_END_HEADERS) + r")\b",
    re.IGNORECASE)

# Markers at the start of a bibliography entry. We accept either
# bracketed ([12]) or dotted (12.) numerals at line start.
_ENTRY_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})\.)\s+(.*)$")

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def _open_doc(pdf_path):
    gfile = Gio.File.new_for_path(pdf_path)
    return Poppler.Document.new_from_gfile(gfile, None, None)


def _page_texts(doc):
    """Return the document text as a list of per-page strings."""
    out = []
    for i in range(doc.get_n_pages()):
        page = doc.get_page(i)
        out.append(page.get_text() or "")
    return out


def _find_bibliography_start(page_texts):
    """Locate the bibliography header. Returns (page_idx, line_idx)
    pointing to the first line *after* the header, or None."""
    for pi, txt in enumerate(page_texts):
        lines = txt.splitlines()
        for li, line in enumerate(lines):
            if _HEADER_RE.match(line):
                return pi, li + 1
    return None


def _collect_bibliography_lines(page_texts, start):
    """From `start = (page_idx, line_idx)`, walk forward across pages,
    stopping at the first end-of-bibliography header. Returns a flat
    list of lines."""
    pi, li = start
    out = []
    while pi < len(page_texts):
        lines = page_texts[pi].splitlines()
        for line in lines[li:]:
            if _END_HEADER_RE.match(line):
                return out
            out.append(line)
        pi += 1
        li = 0
    return out


def _split_into_entries(lines):
    """Group continuation lines onto their leading [N] / N. marker."""
    entries = []
    current = None  # (n, [lines])
    for line in lines:
        m = _ENTRY_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            n = int(m.group(1) or m.group(2))
            current = (n, [m.group(3).strip()])
        else:
            if current is None:
                continue  # text before the first numbered entry
            stripped = line.strip()
            if stripped:
                current[1].append(stripped)
    if current is not None:
        entries.append(current)
    return entries


def _normalize_doi(doi):
    """Trim trailing punctuation that often clings to a DOI scraped
    from a sentence end."""
    if not doi:
        return None
    return doi.rstrip(".,;)]>").lower()


def parse_bibliography(pdf_path):
    """Parse the numbered bibliography of `pdf_path`.

    Returns a list of `{"n": int, "text": str, "doi": str | None}`,
    sorted by `n`. Returns `[]` if no recognisable bibliography
    section is found or the section contains no numbered entries."""
    try:
        doc = _open_doc(pdf_path)
    except Exception:
        return []
    page_texts = _page_texts(doc)
    start = _find_bibliography_start(page_texts)
    if start is None:
        return []
    lines = _collect_bibliography_lines(page_texts, start)
    raw = _split_into_entries(lines)
    out = []
    seen = set()
    for n, parts in raw:
        if n in seen:
            continue  # ignore stray duplicate numbers
        seen.add(n)
        text = " ".join(parts).strip()
        if not text:
            continue
        # Stitch soft-hyphen line wraps. PDF text comes out as one
        # line per visual line, so a DOI like "10.1038/s41598-" /
        # "12345" arrives as "...s41598- 12345" — the DOI regex
        # would stop at the space.
        text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
        m = _DOI_RE.search(text)
        out.append({
            "n": n,
            "text": text,
            "doi": _normalize_doi(m.group(0)) if m else None,
        })
    out.sort(key=lambda r: r["n"])
    return out
