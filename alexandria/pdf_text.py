"""Slice plain text out of a PDF, page range and char-cap aware.

Used by the MCP `get_pdf_texts` tool. Same approach as
`pdb_mentions._pdf_fulltext`: shell out to `pdftotext` from
poppler-utils (already an Alexandria dependency for the import
path), parse the form-feed-delimited output. Page count comes
from `pdfinfo` when available; both binaries ship in the same
poppler-utils package."""

import os
import re
import shutil
import subprocess


_FORM_FEED = "\x0c"
_PAGES_RE = re.compile(r"^Pages:\s+(\d+)$", re.MULTILINE)


def page_count(pdf_path):
    """Total page count via `pdfinfo`. Returns int, or None if
    pdfinfo isn't installed / fails / the file isn't a PDF."""
    if not pdf_path or not os.path.isfile(pdf_path):
        return None
    if not shutil.which("pdfinfo"):
        return None
    try:
        proc = subprocess.run(
            ["pdfinfo", pdf_path],
            capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    m = _PAGES_RE.search(proc.stdout or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def extract_pages(pdf_path, page_from=1, page_to=None,
                  max_chars=50000, timeout=60):
    """Extract plain text from `pdf_path`, restricted to pages
    `[page_from, page_to]` (1-indexed, inclusive; `page_to=None`
    means "to the end of the document") and truncated at
    `max_chars`. Returns `(text, truncated, error)`:

    * `text` — extracted text, or "" on error.
    * `truncated` — True if the page slice was longer than
      `max_chars` and was cut.
    * `error` — None on success, otherwise a short human-readable
      message (binary missing, file missing, pdftotext failure).
    """
    if not pdf_path:
        return "", False, "no pdf_path"
    if not os.path.isfile(pdf_path):
        return "", False, "pdf not found at {}".format(pdf_path)
    if not shutil.which("pdftotext"):
        return "", False, "pdftotext not installed"
    if page_from < 1:
        page_from = 1
    cmd = ["pdftotext", "-f", str(int(page_from))]
    if page_to is not None:
        cmd += ["-l", str(int(page_to))]
    cmd += [pdf_path, "-"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", False, "pdftotext timed out"
    except OSError as e:
        return "", False, "pdftotext failed: {}".format(e)
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip().splitlines()
        return "", False, ("pdftotext exit {}: {}".format(
            proc.returncode, msg[-1] if msg else "unknown"))
    text = proc.stdout or ""
    truncated = False
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    # Form-feed page separators add noise without helping the LLM.
    # Replace with two blank lines so page boundaries are still
    # visible without burning a non-printing byte.
    text = text.replace(_FORM_FEED, "\n\n")
    return text, truncated, None
