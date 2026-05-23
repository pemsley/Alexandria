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

# Author-year entry marker: `<Surname>, <I>.` at line start, where the
# surname can be multi-word ("La Fortelle"), hyphenated ("Sheldrick-
# Smith") or carry diacritics ("Müller"), and is followed by a comma
# and an upper-case initial with its trailing period.
#
# This regex is the *gate* — it only decides "is this line the start
# of a new bibliography entry?". Surname/year extraction happens in a
# second pass on the joined entry text.
_AUTHOR_YEAR_ENTRY_RE = re.compile(
    r"^\s*"
    # Multi-word capitalised surname. Allows "La Fortelle", "van der
    # Berg", "de la Cruz" etc. — particles must be lowercase so we
    # don't accidentally match things like "Materials and Methods".
    r"[A-Z][a-zA-ZÀ-ſ''\-]+"
    r"(?:\s+(?:[a-z][a-zA-ZÀ-ſ''\-]+|[A-Z][a-zA-ZÀ-ſ''\-]+))*"
    r"\s*,\s+"
    # Initial: at least one uppercase letter with trailing period.
    r"[A-Z]\.")

# Surname / year extraction from the joined entry text. The surname
# is everything up to (but not including) the first comma; the year
# is the first 4-digit `(YYYY[a-z]?)` parenthesised in the entry.
_AUTHOR_YEAR_SURNAME_RE = re.compile(
    r"^\s*([A-Z][a-zA-ZÀ-ſ''\- ]+?)\s*,")
_AUTHOR_YEAR_YEAR_RE = re.compile(r"\((\d{4})([a-z])?\)")

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

    Bins x-coordinates to 10pt buckets, keeps the well-populated
    bins, and groups them into clusters separated by gaps > 80pt.
    Returns the leftmost bin of each cluster — one entry for a
    single-column paper, two for a double-column paper, three or
    more when there's also a sidebar / page-side label column.

    Hanging-indent continuations sit ~10–20pt right of their marker
    line, so they fall in the same cluster as the marker and don't
    inflate the edge count. The 80pt gap threshold is large enough
    to clear that, small enough to detect any real gutter."""
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
    edges = [popular[0]]
    for i in range(1, len(popular)):
        # Compare against the immediately-previous bin so a wide
        # cluster (40, 50, 60, 80, 100, 150) stays one cluster —
        # comparing against edges[-1] would let small in-cluster
        # gaps eventually exceed 80pt and spuriously split.
        if popular[i] - popular[i - 1] > 80:
            edges.append(popular[i])
    return edges


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

    # Detect the bibliography style by sampling entry-shaped lines.
    # Numbered ("[12]" or "12.") wins by default; author-year fires
    # when entries look like "Surname, I.…(YYYY)." (Acta Cryst, IUCr,
    # most older crystallography journals). Sampling avoids
    # mis-detecting on a paper with body-text noise above.
    style = _detect_bib_style(bib, edges)

    def _is_marker(rec):
        if style == "author-year":
            if not _AUTHOR_YEAR_ENTRY_RE.match(rec[1]):
                return False
            # Position-aware: marker lines sit at a column-left edge;
            # hanging-indent continuation lines (which often start with
            # a co-author surname like "Wüthrich, K. & Wilson, I. A.")
            # are indented further and must NOT be treated as new
            # entries. Tolerance of 10pt covers normal x rounding plus
            # the 10pt-binned edge values from `_column_edges` (a
            # marker at x=44.8 lands in the bin labelled 40).
            return any(abs(rec[2] - e) <= 10.0 for e in edges)
        return bool(_ENTRY_RE.match(rec[1]))

    # Running headers ("Carbery et al. Journal of Cheminformatics")
    # and journal-issue strings appear at the top of each page. They
    # don't match the marker regex, so without filtering they'd
    # attach as continuation lines to whichever entry was current
    # when the page broke. Drop any non-marker line on a page that
    # sits *above* the first marker on that page.
    page_first_marker_y = {}
    for rec in bib:
        pi, _, _, y1, _, _ = rec
        if _is_marker(rec):
            prev = page_first_marker_y.get(pi)
            if prev is None or y1 < prev:
                page_first_marker_y[pi] = y1
    bib = [
        rec for rec in bib
        if _is_marker(rec)
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
    n_counter = 0
    for rec in bib:
        pi, text, _, y1, _, _ = rec
        if _is_marker(rec):
            if current is not None:
                entries.append(current)
            if style == "author-year":
                n_counter += 1
                n = n_counter
                tail = text.strip()  # whole line is the entry's first chunk
            else:
                m = _ENTRY_RE.match(text)
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
        rec_out = {
            "n": n,
            "text": joined,
            "doi": _normalize_doi(m.group(0)) if m else None,
            "page": pi,
            "y_top_poppler": y_top,
        }
        if style == "author-year":
            sm = _AUTHOR_YEAR_SURNAME_RE.match(joined)
            ym = _AUTHOR_YEAR_YEAR_RE.search(joined)
            if sm and ym:
                surname = sm.group(1).strip()
                year = ym.group(1)
                suffix = ym.group(2) or ""
                rec_out["surname"] = surname
                rec_out["year"] = year
                rec_out["suffix"] = suffix
                rec_out["key"] = "{}{}{}".format(
                    surname.lower(), year, suffix)
                # Journal: text between the year and the first volume
                # digit. Acta Cryst entries have no title, so this
                # journal field is what `find_doi_by_author_year`
                # uses to disambiguate same-surname-same-year hits.
                after = joined[ym.end():].lstrip(". ").strip()
                vol_m = re.search(r"\d", after)
                if vol_m:
                    journal_raw = after[:vol_m.start()]
                else:
                    journal_raw = after
                # Keep the trailing period — it marks the last token
                # as an abbreviation, which `metrics._expand_
                # journal_abbreviations` needs to match "Cryst." to
                # "Crystallography". Stripping it produced "J. Appl.
                # Cryst" which left "Cryst" unexpanded.
                journal = journal_raw.strip().rstrip(",").strip()
                rec_out["journal"] = journal or None
        out.append(rec_out)
    if style == "author-year":
        _disambiguate_author_year(out)
    out.sort(key=lambda r: r["n"])
    return out


def _detect_bib_style(bib, edges):
    """Decide whether `bib` is a numbered or author-year bibliography,
    based on how many sample entry-shaped lines match each pattern.
    Sampling rather than first-match because publishers occasionally
    sneak a stray line at the top that fits the wrong style. Defaults
    to 'numbered' on a tie or when neither matches."""
    n_numbered = 0
    n_author_year = 0
    for rec in bib[:60]:
        text = rec[1]
        if _ENTRY_RE.match(text):
            n_numbered += 1
            continue
        if _AUTHOR_YEAR_ENTRY_RE.match(text):
            # Position-aware: only count column-edge lines, so we
            # don't over-count hanging-indent continuations.
            if any(abs(rec[2] - e) <= 10.0 for e in edges):
                n_author_year += 1
    return "author-year" if n_author_year > n_numbered else "numbered"


def _disambiguate_author_year(entries):
    """When several entries share the same (surname, year) without an
    explicit a/b/c suffix in the parenthesised year, assign suffixes
    in document order. Mutates `entries` in place. Entries whose year
    already carries a suffix from the source PDF are left alone."""
    by_base = {}
    for e in entries:
        if "surname" not in e or e.get("suffix"):
            continue
        base = (e["surname"].lower(), e["year"])
        by_base.setdefault(base, []).append(e)
    for base, group in by_base.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (r.get("page", 0), r.get("y_top_poppler", 0)))
        for i, e in enumerate(group):
            e["suffix"] = chr(ord("a") + i)
            e["key"] = "{}{}{}".format(
                e["surname"].lower(), e["year"], e["suffix"])


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


# --- Author-year citation hit-testing in the body text ---------------
#
# Phase 2 of the Acta Cryst / IUCr support: walk each page's text for
# `(Surname, YYYY)` and `Surname (YYYY)` citation patterns, look each
# up against the parsed bibliography, and return citation_links in
# the same shape `pdf_links.read_citation_links` produces.
# `find_author_year_citations` is the entry point.

# A capitalised, possibly multi-word, possibly accented surname.
# Particles (de, del, von, van, der, …) are lowercase; main words
# capitalise. "La Fortelle", "van der Berg", "de la Cruz" all match.
_SURNAME = r"[A-Z][\wÀ-ſ''\-]+(?:\s+(?:[a-z][\wÀ-ſ''\-]+|[A-Z][\wÀ-ſ''\-]+))*"

# A single (surname [+ et al. / and Other / & Other], year) piece
# inside a parenthesised citation. Captures the leading surname
# (group 1) and the year-with-optional-suffix (group 2). The piece
# may continue past the end of the match (extra years on a multi-
# year cite like "Smith, 2020a, 2020b" are picked up by a separate
# all-years scan).
_AY_PIECE_RE = re.compile(
    r"^\s*(%s)"
    r"(?:\s+et\s+al\.?|\s+and\s+%s|\s+&\s+%s)?"
    r",?\s+(\d{4}[a-z]?)\b" % (_SURNAME, _SURNAME, _SURNAME))

# Parenthetical citation: any `(...)` containing a 4-digit year. We
# require no nested parens in the body so we don't wander into
# `... (Smith (1999) ...)`. The 300-char body cap stops the regex
# from greedily eating across columns when poppler's text reflow
# accidentally elides a closing paren.
_AY_PAREN_RE = re.compile(
    r"\(([^()]{0,300}?\b\d{4}[a-z]?\b[^()]{0,300}?)\)")

# Narrative citation: `Surname (YYYY)` / `Surname et al. (YYYY)` /
# `Surname and Other (YYYY)`. The negative lookbehind keeps us off
# of in-word matches like "Cabsmith (2020)" inside a longer word.
_AY_NARRATIVE_RE = re.compile(
    r"(?<![A-Za-zÀ-ſ])"
    r"(%s(?:\s+et\s+al\.?|\s+and\s+%s|\s+&\s+%s)?)"
    r"\s+\((\d{4}[a-z]?)\)" % (_SURNAME, _SURNAME, _SURNAME))

# Extra year tokens within a piece, for multi-year cites like
# "Smith, 2020a, 2020b" or "Smith 2020 and 2021".
_AY_YEAR_RE = re.compile(r"\b(\d{4}[a-z]?)\b")


def _spans_to_rect(rects, start, end, page_height):
    """Compute a bounding rect over `rects[start:end]`, returned in
    PDF user space (origin bottom-left, y up) — the convention
    `pdf_links.read_citation_links` uses, so the viewer's
    `_citation_at` hit-tester works on it unchanged. Returns None
    when the span is empty or any rect is degenerate."""
    if start >= end or end > len(rects):
        return None
    xs1, ys1, xs2, ys2 = [], [], [], []
    for r in rects[start:end]:
        xs1.append(r.x1)
        xs2.append(r.x2)
        ys1.append(r.y1)
        ys2.append(r.y2)
    if not xs1:
        return None
    x_lo = min(xs1)
    x_hi = max(xs2)
    # poppler coords have origin top-left, y growing down. Flip to
    # PDF user-space (origin bottom-left, y up).
    y_pop_top = min(ys1)
    y_pop_bot = max(ys2)
    return (x_lo, page_height - y_pop_bot, x_hi, page_height - y_pop_top)


def _find_year_offsets(text, span_start, span_end):
    """Yield (year_str, year_start, year_end) for each 4-digit year
    inside `text[span_start:span_end]`. Offsets are absolute (into
    `text`). Used to assign per-year rects on multi-year cites."""
    for m in _AY_YEAR_RE.finditer(text, span_start, span_end):
        yield m.group(1), m.start(), m.end()


def _build_bib_lookup(bib_entries):
    """Return `(by_key, by_surname_year)`.

    `by_key` is keyed by the canonical `surname.lower()+year+suffix`
    string — exact match against a citation's parsed key. Used when
    the citation explicitly carries a suffix ("Smith, 2020a") that
    matches the bibliography.

    `by_surname_year` is keyed by `(surname.lower(), year)` with no
    suffix; values are the list of entries sharing that base. Used
    for plain-year citations ("Smith, 2020") that we can match
    unambiguously when only one bib entry has that base."""
    by_key = {}
    by_surname_year = {}
    for e in bib_entries:
        key = e.get("key")
        if not key:
            continue
        by_key[key] = e
        base = (e["surname"].lower(), e["year"])
        by_surname_year.setdefault(base, []).append(e)
    return by_key, by_surname_year


def _match_bib(surname, year, by_key, by_surname_year):
    """Resolve a (surname, year) citation to a bibliography entry,
    or None when the lookup is ambiguous or absent. Surname matching
    folds to lowercase; year may carry an `a/b/c` suffix.

    The first surname token wins on multi-author citations
    ("Smith and Jones, 2020" → look up "smith"); that's how
    `parse_bibliography` keys its entries too."""
    surname_lc = surname.strip().lower()
    # A citation like "Smith and Jones, 2020" arrives with surname
    # already trimmed to "Smith" by _AY_PIECE_RE's first capturing
    # group. Defensive: if the caller didn't trim, take the first
    # whitespace-bounded token.
    surname_lc = surname_lc.split()[0] if surname_lc else surname_lc
    # Re-add particle support. parse_bibliography keeps multi-word
    # surnames intact ("la fortelle"); fold the citation's surname
    # the same way by recovering the raw form.
    full_lc = surname.strip().lower()
    # First: exact key match (citation has explicit suffix).
    has_suffix = bool(year) and year[-1].isalpha()
    if has_suffix:
        key = "{}{}".format(full_lc, year)
        e = by_key.get(key)
        if e:
            return e
        # Try first-token only.
        key2 = "{}{}".format(surname_lc, year)
        e = by_key.get(key2)
        if e:
            return e
        return None
    # Plain year — match against (surname, year) base. If exactly
    # one entry exists, use it (suffix or no suffix in the bib).
    for sn in (full_lc, surname_lc):
        candidates = by_surname_year.get((sn, year))
        if candidates and len(candidates) == 1:
            return candidates[0]
    return None


def find_author_year_citations(pdf_path, bib_entries):
    """Find author-year citations in the body text of `pdf_path`
    and return them in the same shape as
    `pdf_links.read_citation_links`:
    `{page_idx: [(rect, target_page, target_top, ref_n), ...]}`.

    Hit-tests support both `(Surname, YYYY)` parenthetical and
    `Surname (YYYY)` narrative forms, including `et al.`,
    `and Other`, `&`, multi-citation tokens (`(Smith, 2020;
    Jones, 2003)`) and multi-year tokens (`(Smith, 2020a, 2020b)`).

    `bib_entries` is the list returned by `parse_bibliography`. Only
    entries that carry a `key` (i.e. came out of the author-year
    branch) are considered. Returns `{}` when the bibliography is
    empty or numbered.

    `ref_n` in the returned tuples is the entry's positional
    integer `n`, so the viewer's existing
    `_show_reference_popover` flow keeps working unchanged — it
    consumes `ref_n` as a key into `_bibliography_by_n`."""
    if not bib_entries:
        return {}
    by_key, by_surname_year = _build_bib_lookup(bib_entries)
    if not by_key:
        return {}
    try:
        doc = _open_doc(pdf_path)
    except Exception:
        return {}

    out = {}
    for pi in range(doc.get_n_pages()):
        page = doc.get_page(pi)
        text = page.get_text() or ""
        if not text:
            continue
        res = page.get_text_layout()
        if isinstance(res, tuple):
            ok, rects = res
        else:
            rects = res
            ok = rects is not None
        if not ok or rects is None:
            continue
        # Length-align text to rects — same dance _page_lines does.
        if len(rects) == len(text):
            text_aligned = text
        else:
            no_nl = text.replace("\n", "")
            if len(rects) == len(no_nl):
                text_aligned = no_nl
            else:
                continue
        _, page_h = page.get_size()
        page_links = []

        # Parenthetical: scan for `(...year...)` tokens, then split
        # each one's inner content into per-piece sub-rects.
        for m in _AY_PAREN_RE.finditer(text_aligned):
            inner = m.group(1)
            inner_start = m.start(1)  # absolute offset of inner text
            # Split inner on `;` — each segment is one citation
            # (multi-citation form). For each segment, find a
            # leading (surname, [et al / and / &], year) and any
            # additional years for multi-year cites.
            seg_off = 0
            for seg in inner.split(";"):
                seg_abs_start = inner_start + seg_off
                seg_off += len(seg) + 1  # +1 for the ';'
                pm = _AY_PIECE_RE.match(seg)
                if not pm:
                    continue
                surname = pm.group(1)
                # Collect every year in this segment so multi-year
                # cites ("Smith, 2020a, 2020b") emit one link per
                # year, each scoped to its own rect.
                seg_text = seg
                for year, rel_y_start, rel_y_end in _find_year_offsets(
                        seg_text, 0, len(seg_text)):
                    entry = _match_bib(
                        surname, year, by_key, by_surname_year)
                    if entry is None:
                        continue
                    abs_start = seg_abs_start + pm.start(1)
                    abs_end = seg_abs_start + rel_y_end
                    rect = _spans_to_rect(
                        rects, abs_start, abs_end, page_h)
                    if rect is None:
                        continue
                    target_pi = entry.get("page")
                    y_pop = entry.get("y_top_poppler")
                    if target_pi is None or y_pop is None:
                        continue
                    try:
                        _, target_h = doc.get_page(target_pi).get_size()
                    except Exception:
                        continue
                    target_top = target_h - y_pop
                    page_links.append(
                        (rect, target_pi, target_top, entry["n"]))

        # Narrative: `Surname (YYYY)` / `Surname et al. (YYYY)`.
        for m in _AY_NARRATIVE_RE.finditer(text_aligned):
            surname_part = m.group(1)
            year = m.group(2)
            # Pull out the leading surname only (before any "et al."
            # or "and" / "&"), so plural-form cites still resolve.
            sm = re.match(_SURNAME, surname_part)
            if not sm:
                continue
            surname = sm.group(0)
            entry = _match_bib(surname, year, by_key, by_surname_year)
            if entry is None:
                continue
            rect = _spans_to_rect(rects, m.start(), m.end(), page_h)
            if rect is None:
                continue
            target_pi = entry.get("page")
            y_pop = entry.get("y_top_poppler")
            if target_pi is None or y_pop is None:
                continue
            try:
                _, target_h = doc.get_page(target_pi).get_size()
            except Exception:
                continue
            target_top = target_h - y_pop
            page_links.append(
                (rect, target_pi, target_top, entry["n"]))

        if page_links:
            out[pi] = page_links
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
