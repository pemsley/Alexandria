"""Extract a paper's bibliography from its PDF using poppler-glib.

v1 scope: numbered references only ([N] or N. style). Returns a list
of {n, text, doi} dicts; the viewer can hit-test [N] markers in the
body and look up the matching entry. Author-year styles ("Smith,
2020") are deliberately out of scope — they're a much messier
problem and best layered on top once the numbered case is solid.

In-text citations like "[9-12]" or "[1, 3, 5-7]" are split into
individual entry numbers by `expand_citation_token`; the
bibliography itself only contains one entry per number, so the
range expansion lives at the lookup layer, not here.
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
    "publisher's note",
    "publisher’s note",
    "competing interests",
    "author contributions",
    "data availability",
    "ethics declarations",
)
_END_HEADER_RE = re.compile(
    r"^\s*(?:\d+[\.\)]?\s+)?(" + "|".join(_END_HEADERS) + r")\b",
    re.IGNORECASE)

# Running headers / footers that survive into the text stream and
# would otherwise attach as continuation lines to the previous
# bibliography entry. We drop these outright before walking entries.
_NOISE_RE = re.compile(
    r"^\s*(?:"
    r"page\s+\d+\s+of\s+\d+"            # "Page 17 of 17"
    r"|\(\d{4}\)\s+\d+\s*:\s*\d+"       # Springer running head "(2024) 16:32"
    r")\s*$",
    re.IGNORECASE)

# Bibliography entry markers: bracketed ([12]) or dotted/parenthesised
# (12. or 12)) numerals at line start. Hanging-indent layouts often
# render the marker on its own visual line ("12." with nothing after),
# so the body part is allowed to be empty.
_ENTRY_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[\.\)])\s*(.*)$")

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)

# Stitches DOI fragments split across lines (e.g. "10.1234/abc def"
# from a wrapped URL) — applied iteratively until stable.
_FRAGMENT_DOI_RE = re.compile(
    r"(10\.\d{4,9}/[-._;()/:A-Z0-9]*?)\s+([-._;()/:A-Z0-9])",
    re.IGNORECASE,
)


def _open_doc(pdf_path):
    gfile = Gio.File.new_for_path(pdf_path)
    return Poppler.Document.new_from_gfile(gfile, None, None)


def _page_lines(page):
    """Return [(text, x1, y1, x2, y2), ...] for one page's visual
    lines. Coordinates are in PDF points with the origin at the top
    of the page (poppler-glib convention).

    Uses get_text_layout() so column-aware reading order can be
    reconstructed; falls back to positionless lines if the layout
    array can't be aligned with get_text()."""
    text = page.get_text() or ""
    res = page.get_text_layout()
    if isinstance(res, tuple):
        ok, rects = res
    else:
        rects = res
        ok = rects is not None
    if not ok or rects is None:
        return _fallback_lines(text)
    if len(rects) == len(text):
        return _zip_into_lines(text, rects, split_on_newline=True)
    no_nl = text.replace("\n", "")
    if len(rects) == len(no_nl):
        return _zip_into_lines(no_nl, rects, split_on_newline=False)
    return _fallback_lines(text)


def _fallback_lines(text):
    return [(line, 0.0, float(i), 0.0, float(i + 1))
            for i, line in enumerate(text.splitlines())]


def _zip_into_lines(text, rects, split_on_newline):
    """Group character/rect pairs into lines. When poppler emits
    explicit '\\n' characters we split on those; otherwise we split
    on a vertical jump in the y-coordinate.

    We *also* split on a large y-jump even in newline mode, because
    poppler occasionally elides the newline between the bottom of
    one column and the top of the next on the same page. Without
    this fallback those two physical lines fuse, the merged line's
    y is reported as the minimum (top of the second column), and
    the parser attributes the second column's leading text to a
    spatially-distant entry — the PNAS bibliography hits this on
    the col-0-to-col-1 wrap."""
    BIG_Y_JUMP = 30.0  # bigger than normal line spacing (~8-15pt)
    out = []
    cur = []
    last_y = None
    for ch, r in zip(text, rects):
        if split_on_newline and ch == "\n":
            _flush_line(cur, out)
            cur = []
            last_y = None
            continue
        if last_y is not None:
            small_jump = abs(r.y1 - last_y) > 4.0
            if (not split_on_newline) and small_jump:
                _flush_line(cur, out)
                cur = []
            elif split_on_newline and abs(r.y1 - last_y) > BIG_Y_JUMP:
                _flush_line(cur, out)
                cur = []
        cur.append((ch, r))
        last_y = r.y1
    _flush_line(cur, out)
    return out


# Zero-width characters that some publishers (Springer in particular)
# inject between every glyph of a hyperlinked DOI as wrap hints. They
# don't render visibly but break our DOI regex.
_ZERO_WIDTH = "".join(["​", "‌", "‍", "﻿", "­"])
_ZERO_WIDTH_TRANS = str.maketrans("", "", _ZERO_WIDTH)


def _flush_line(chars, out):
    if not chars:
        return
    line_text = "".join(ch for ch, _ in chars)
    line_text = line_text.translate(_ZERO_WIDTH_TRANS).rstrip()
    if not line_text.strip():
        return
    xs1 = [r.x1 for _, r in chars]
    ys1 = [r.y1 for _, r in chars]
    xs2 = [r.x2 for _, r in chars]
    ys2 = [r.y2 for _, r in chars]
    out.append((line_text, min(xs1), min(ys1), max(xs2), max(ys2)))


def _doc_lines(doc):
    """[(page_idx, text, x1, y1, x2, y2), ...] across all pages."""
    out = []
    for pi in range(doc.get_n_pages()):
        for line in _page_lines(doc.get_page(pi)):
            out.append((pi,) + line)
    return out


def _column_edges(lines):
    """Cluster line-x1 values to recover column left edges.

    Returns a sorted list — one entry for single-column, two for
    double-column. We bin x-coordinates and keep only well-populated
    bins, so stray page-numbers and centred headers (which sit at
    odd x positions) don't manufacture a fake gutter. The largest
    gap among popular bins must be both wide (>80pt) and clearly
    bigger than any within-column gap (>3x) before we call it a
    two-column layout — otherwise a typical hanging indent of
    15-20pt could masquerade as a column boundary."""
    if not lines:
        return []
    if len(lines) < 5:
        return [min(rec[2] for rec in lines)]
    bins = {}
    for rec in lines:
        key = int(rec[2] // 10) * 10
        bins[key] = bins.get(key, 0) + 1
    threshold = max(3, int(len(lines) * 0.03))
    popular = sorted(k for k, c in bins.items() if c >= threshold)
    if not popular:
        return [min(rec[2] for rec in lines)]
    if len(popular) < 2:
        return [popular[0]]
    gaps = sorted(
        ((popular[i + 1] - popular[i], i) for i in range(len(popular) - 1)),
        reverse=True,
    )
    biggest, idx = gaps[0]
    second = gaps[1][0] if len(gaps) > 1 else 0.0
    if biggest > 80.0 and biggest > 3.0 * max(second, 1.0):
        return [popular[0], popular[idx + 1]]
    return [popular[0]]


def _column_index(x, edges):
    if len(edges) <= 1:
        return 0
    return min(range(len(edges)), key=lambda i: abs(x - edges[i]))


def _sort_reading_order(lines, edges):
    return sorted(
        lines,
        key=lambda rec: (rec[0], _column_index(rec[2], edges), rec[3]),
    )


def _stitch_wrapped(text):
    """Reassemble tokens that line-wrapping split with a space.
    Soft-hyphen wraps (`s41598-` / `12345`) come first; then any
    space inside a `10.NNNN/...` DOI body is collapsed iteratively."""
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    prev = None
    while prev != text:
        prev = text
        text = _FRAGMENT_DOI_RE.sub(r"\1\2", text)
    return text


def _normalize_doi(doi):
    if not doi:
        return None
    return doi.rstrip(".,;)]>").lower()


def _find_bibliography_fallback(lines, min_run=5):
    """Locate the bibliography in PDFs that have no "References"
    heading (older PNAS, some Cell Press, …) by finding the longest
    *strictly-contiguous* run of sequentially-numbered entry markers
    in document order: candidates[i].n == 1, candidates[i+1].n == 2,
    candidates[i+2].n == 3, ... with no intervening markers.

    Strict contiguity is what disambiguates real bibliographies from
    stray body-text markers like an in-paragraph "1)". A spurious
    "1)" is followed in document order by other body markers (Eq.
    refs, footnotes, citation tokens) before the real bib's "2."
    arrives, so its contiguous run length is 1, while the real bib's
    is the entry count.

    Returns `(page, x, y)` of the marker for entry 1 of that run,
    or `None` if no run of at least `min_run` entries was found."""
    candidates = []  # (n, page, x, y)
    for rec in lines:
        m = _ENTRY_RE.match(rec[1])
        if m:
            n = int(m.group(1) or m.group(2))
            candidates.append((n, rec[0], rec[2], rec[3]))
    if not candidates:
        return None
    best_start = None
    best_len = 0
    for start in range(len(candidates)):
        if candidates[start][0] != 1:
            continue
        run_len = 1
        # Strict contiguity: each next candidate must be the next
        # integer in sequence. Stop at the first gap or out-of-order
        # candidate. This rejects body-text "1)" because the next
        # candidate in document order is rarely "2)".
        for j in range(start + 1, len(candidates)):
            if candidates[j][0] == run_len + 1:
                run_len += 1
            else:
                break
        if run_len > best_len:
            best_len = run_len
            best_start = start
    if best_start is None or best_len < min_run:
        return None
    _, page, x, y = candidates[best_start]
    return page, x, y


def parse_bibliography(pdf_path):
    """Parse the numbered bibliography of `pdf_path`.

    Returns a list of `{"n": int, "text": str, "doi": str | None,
    "page": int, "y_top_poppler": float}`, sorted by `n`. Returns
    `[]` if no recognisable bibliography section is found or the
    section contains no numbered entries."""
    try:
        doc = _open_doc(pdf_path)
    except Exception:
        return []
    lines = _doc_lines(doc)
    if not lines:
        return []

    # Find the bibliography header by spatial position. Two-column
    # journals often place "References" in column 1 with article
    # body still flowing in column 2 above where refs start, so we
    # need both the header's column and y to know what counts as bib.
    header_page = None
    header_x = None
    header_y = None
    for rec in lines:
        if _HEADER_RE.match(rec[1]):
            header_page, _, header_x, header_y, _, _ = rec
            break
    fallback_mode = False
    if header_page is None:
        # Headerless layout (older PNAS, etc.) — locate the
        # bibliography by sequential-marker scan instead. Then take
        # everything in reading order from entry 1 onwards (rather
        # than the headered path's spatial filter, which assumes the
        # bibliography occupies a clean rectangle below `header_y`
        # and isn't sandwiched between body-text columns).
        fallback = _find_bibliography_fallback(lines)
        if fallback is None:
            return []
        header_page, header_x, header_y = fallback
        fallback_mode = True

    candidate_all = [rec for rec in lines if rec[0] >= header_page]
    edges = _column_edges(candidate_all)
    header_col = _column_index(header_x, edges)
    sorted_all = _sort_reading_order(candidate_all, edges)

    if fallback_mode:
        # No "References" heading. We've located the marker for
        # entry 1; collect every marker at-or-after that position in
        # reading order to derive a per-(page, col) y-band that
        # covers the bibliography. Markers don't have to be
        # strictly sequential here — older PNAS papers occasionally
        # skip a number (e.g. 28 → 30) — but they do all live
        # below the located start.
        #
        # The per-(page, col) y-band is necessary because PNAS-style
        # 2-column papers run body text down both columns and start
        # the bibliography below that in *both* columns. A naive
        # "everything from entry 1 onwards in reading order" sweeps
        # in body text at the top of column 1.
        start_col = _column_index(header_x, edges)
        markers = []   # (n, page, col, y)
        for rec in sorted_all:
            m = _ENTRY_RE.match(rec[1])
            if not m:
                continue
            col = _column_index(rec[2], edges)
            # at-or-after the located start in reading order
            if rec[0] < header_page:
                continue
            if rec[0] == header_page:
                if col < start_col:
                    continue
                if col == start_col and rec[3] < header_y - 0.5:
                    continue
            n = int(m.group(1) or m.group(2))
            markers.append((n, rec[0], col, rec[3]))
        if not markers:
            return []
        by_pcol = {}
        for _n, pg, col, y in markers:
            by_pcol.setdefault((pg, col), []).append(y)
        # Slack of 80pt past the last marker in each column captures
        # the continuation lines of that column's final entry.
        y_range = {pcol: (min(ys) - 0.5, max(ys) + 80.0)
                   for pcol, ys in by_pcol.items()}
        candidate = []
        for rec in sorted_all:
            col = _column_index(rec[2], edges)
            rng = y_range.get((rec[0], col))
            if rng is None:
                continue
            y_min, y_max = rng
            if y_min <= rec[3] <= y_max:
                candidate.append(rec)
    else:
        def in_bib_region(rec):
            pi, _, x1, y1, _, _ = rec
            if pi > header_page:
                return True
            col = _column_index(x1, edges)
            if col > header_col:
                return True
            return col == header_col and y1 > header_y

        candidate = [rec for rec in sorted_all if in_bib_region(rec)]

    cutoff = len(candidate)
    for j, rec in enumerate(candidate):
        if _END_HEADER_RE.match(rec[1]):
            cutoff = j
            break
    bib = candidate[:cutoff]
    if not bib:
        return []

    # Running headers ("Carbery et al. Journal of Cheminformatics")
    # and journal-issue strings appear at the top of each page. They
    # don't match _ENTRY_RE, so without filtering they'd attach as
    # continuation lines to whichever entry was current when the page
    # broke. Drop any non-marker line on a page that sits *above* the
    # first marker on that page.
    page_first_marker_y = {}
    for rec in bib:
        pi, text, _, y1, _, _ = rec
        if _ENTRY_RE.match(text):
            prev = page_first_marker_y.get(pi)
            if prev is None or y1 < prev:
                page_first_marker_y[pi] = y1
    bib = [
        rec for rec in bib
        if _ENTRY_RE.match(rec[1])
        or rec[0] not in page_first_marker_y
        or rec[3] >= page_first_marker_y[rec[0]]
    ]
    bib = [rec for rec in bib if not _NOISE_RE.match(rec[1])]

    # Walk markers and accumulate continuation lines. With reading
    # order correct, hanging-indent markers ("5." alone on a line)
    # land immediately before their body lines, so the marker opens
    # the entry and the next non-marker line(s) attach to it. We also
    # record the (page, y_top_poppler) of the *marker* line for each
    # entry — that lets `bibliography_positions()` synthesise PDF-
    # user-space coordinates that pdf_links can match destinations
    # against.
    entries = []
    current = None
    for rec in bib:
        pi, text, _, y1, _, _ = rec
        m = _ENTRY_RE.match(text)
        if m:
            if current is not None:
                entries.append(current)
            n = int(m.group(1) or m.group(2))
            tail = m.group(3).strip()
            current = (n, [tail] if tail else [], pi, y1)
        elif current is not None:
            stripped = text.strip()
            if stripped:
                current[1].append(stripped)
    if current is not None:
        entries.append(current)

    out = []
    seen = set()
    for n, parts, pi, y_top in entries:
        if n in seen:
            continue
        seen.add(n)
        joined = " ".join(parts).strip()
        if not joined:
            continue
        joined = _stitch_wrapped(joined)
        m = _DOI_RE.search(joined)
        out.append({
            "n": n,
            "text": joined,
            "doi": _normalize_doi(m.group(0)) if m else None,
            "page": pi,
            "y_top_poppler": y_top,
        })
    out.sort(key=lambda r: r["n"])
    return out


def bibliography_positions(pdf_path):
    """Return marker positions for a parsed bibliography in PDF
    user space — `[(n, page_idx, y_pdf), ...]` — where `y_pdf` has
    the origin at the bottom-left of the page (PDF convention) so
    callers can match these against the `target_top` field of
    `pdf_links.read_citation_links` directly without coordinate-
    system conversions.

    Used by `pdf_links.assign_ref_n_by_position` to recover the
    reference number for Link annotations whose destination name
    doesn't follow a recognisable `CR<N>` pattern (e.g. Taylor &
    Francis's `Anchor N` scheme). Returns `[]` when the
    bibliography can't be parsed."""
    try:
        doc = _open_doc(pdf_path)
    except Exception:
        return []
    refs = parse_bibliography(pdf_path)
    if not refs:
        return []
    page_h = {}
    out = []
    for r in refs:
        pi = r.get("page")
        y_pop = r.get("y_top_poppler")
        if pi is None or y_pop is None:
            continue
        if pi not in page_h:
            try:
                _, h = doc.get_page(pi).get_size()
            except Exception:
                continue
            page_h[pi] = h
        h = page_h[pi]
        out.append((r["n"], pi, h - y_pop))
    return out


_CITATION_TOKEN_RE = re.compile(r"\[\s*([\d\s,\-–—]+)\s*\]")


def expand_citation_token(token):
    """Expand an in-text citation like '[9-12]' or '[1, 3, 5-7]'
    into the explicit list of bibliography entry numbers it points
    to. Accepts the surrounding brackets or just the inner body.
    Unparseable parts are skipped silently.

    Used by the viewer to map a clicked citation in the body of the
    paper to one or more bibliography entries; bibliography entries
    themselves don't use ranges."""
    m = _CITATION_TOKEN_RE.search(token) if "[" in token else None
    body = m.group(1) if m else token
    body = body.replace("–", "-").replace("—", "-")
    nums = []
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = (int(x.strip()) for x in part.split("-", 1))
            except ValueError:
                continue
            if lo <= hi:
                nums.extend(range(lo, hi + 1))
        else:
            try:
                nums.append(int(part))
            except ValueError:
                continue
    return nums
