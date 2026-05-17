"""Page PNG thumbnails via pdftoppm (poppler-utils).

Avoids a Python dependency on pdf2image / PIL.

Title-page heuristic: the thumbnail should show the page that
carries the paper's *title* — that's how a human recognises the
paper at a glance, and it's why a graphical-abstract first page is
a great thumbnail (the title is on it) while a sparse journal-cover
first page is a poor one (the title is only on page 2). We already
extract the title at import time, so we use it as ground truth:

  * title found on page 1  → use page 1 (covers the normal
    content-first-page *and* the graphical-abstract case)
  * title only on page 2   → page 1 was a cover/banner; use page 2
  * neither / no title     → fall back to page 1

Matching is fuzzy (token overlap, not literal substring) because
pdftotext line-wraps, hyphenates and de-ligatures. The rule is
deliberately conservative: we only switch *away* from page 1 when
page 2 is a confident title match, so an image-only graphical-
abstract page (whose title pdftotext can't see) still safely keeps
page 1.

Scratch renders are written to a private temp directory *outside*
the watched library tree; only the final PNG is moved into place,
so the library's GFileMonitor sees exactly one create event per
thumbnail instead of a storm of `.pN.png` churn.
"""

import os
import re
import shutil
import subprocess
import tempfile

# Fraction of the title's tokens that must appear on a page for it
# to count as "the title is on this page". 0.7 tolerates pdftotext
# line-wrap / hyphenation noise while still rejecting a page that
# merely shares a couple of common words.
_TITLE_MATCH_RATIO = 0.70
# Below this many title tokens, demand *all* of them (a 2-word
# title can't afford a 0.7 partial match without becoming trivial).
_TITLE_MIN_TOKENS_FOR_RATIO = 4


def _tokens(s):
    """Lowercased alphanumeric token list. Used for both the title
    and the page text so the comparison is wrap/hyphenation-robust."""
    if not s:
        return []
    return re.findall(r"[a-z0-9]+", s.lower())


def _page_text(pdf_path, page):
    """Plain text of a single 1-based page via pdftotext, or ""."""
    try:
        out = subprocess.run(
            ["pdftotext", "-f", str(page), "-l", str(page),
             "-enc", "UTF-8", pdf_path, "-"],
            capture_output=True, timeout=20, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", errors="replace")


def _title_on_page(title, pdf_path, page):
    """True if `title`'s tokens are present on `page` at the
    `_TITLE_MATCH_RATIO` threshold. Conservative: no title, too few
    title tokens, or no page text → False (caller then keeps the
    page-1 default rather than switching pages on a weak signal)."""
    t_tokens = _tokens(title)
    if not t_tokens:
        return False
    page_tokens = set(_tokens(_page_text(pdf_path, page)))
    if not page_tokens:
        return False
    present = sum(1 for tok in t_tokens if tok in page_tokens)
    if len(t_tokens) < _TITLE_MIN_TOKENS_FOR_RATIO:
        return present == len(t_tokens)
    return (present / len(t_tokens)) >= _TITLE_MATCH_RATIO


def _render_page(pdf_path, page, dest_root, width):
    """Render a single 1-based `page` of `pdf_path` to
    `<dest_root>.png` at the given pixel width. Returns the produced
    path on success, or None. `-singlefile` makes pdftoppm append
    just ".png" (no "-N" page-number suffix)."""
    try:
        subprocess.run(
            ["pdftoppm", "-singlefile", "-png",
             "-scale-to-x", str(width), "-scale-to-y", "-1",
             "-f", str(page), "-l", str(page),
             pdf_path, dest_root],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    produced = dest_root + ".png"
    if not os.path.isfile(produced) or os.path.getsize(produced) <= 0:
        return None
    return produced


def make_thumbnail(pdf_path, out_path, width=240, title=None):
    """Render a representative page of `pdf_path` to `out_path` as a
    PNG at the given pixel width. Picks the page that carries the
    paper's `title`; falls back to page 1 when the title is unknown,
    unmatchable, or only present on page 1. Returns True on success.

    All scratch rendering happens in a private temp directory, so
    the library directory only ever sees the single final file
    appear (no `.pN.png` churn for the watcher to react to)."""
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return True

    tmpdir = tempfile.mkdtemp(prefix="pdforg-thumb-")
    try:
        p1 = _render_page(pdf_path, 1, os.path.join(tmpdir, "p1"), width)
        if p1 is None:
            return False

        chosen = p1
        # Only consider page 2 when the title is NOT already on
        # page 1 (so graphical-abstract / normal content first
        # pages keep page 1). Switch only on a confident page-2
        # title match.
        if title and not _title_on_page(title, pdf_path, 1):
            p2 = _render_page(pdf_path, 2, os.path.join(tmpdir, "p2"), width)
            if p2 is not None and _title_on_page(title, pdf_path, 2):
                chosen = p2

        try:
            # shutil.move (not os.replace): tmpdir may be on a
            # different filesystem (tmpfs) than the library.
            # Removes any stale destination first so move can't
            # fail on an existing path.
            if os.path.exists(out_path):
                os.remove(out_path)
            shutil.move(chosen, out_path)
        except OSError:
            return False
        return os.path.isfile(out_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
