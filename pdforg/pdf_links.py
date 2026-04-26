"""Read citation-style /Link annotations from a PDF.

Modern publisher PDFs embed a /Link annotation on each in-text
citation token (`[1]`, `[1,2]`, ...) whose destination points to the
matching bibliography entry. Springer-shaped destinations are named
`<docname>:CR<N>:<seq>` where CR<N> is the reference number; other
publishers use different naming conventions, but the Link → /Dest
machinery is the same.

Returns the data the viewer needs to make those tokens clickable
without parsing the PDF text — a one-shot read at PDF-open time
that produces a per-page list of `(rect, target_page, target_top,
ref_n)` tuples.

When a PDF has no such annotations (older Wiley/iText reprocessed
files, scans, etc.), `read_citation_links` returns `{}` and the
viewer's parser-driven fallback can take over."""

import re

try:
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject, ArrayObject
    _HAVE_PYPDF = True
except ImportError:
    _HAVE_PYPDF = False


# Citation-reference dest names use a few publisher-specific
# patterns, all of them carrying `CR<N>` where N is the bibliography
# entry number:
#   Springer/InDesign: "13321_2024_821_Article.indd:CR12:103"
#   Nature:            "bm_CR1"
#   (more to add as we see them)
# We accept `CR<digits>` preceded by any non-alphanumeric separator
# (colon, underscore, hyphen) — this catches both shapes above and
# keeps random `…CR12…` substrings from matching a regular word.
_CR_RE = re.compile(r"(?:^|[^A-Za-z0-9])CR(\d+)\b")


def _deref(x):
    if _HAVE_PYPDF and isinstance(x, IndirectObject):
        return x.get_object()
    return x


def _walk_name_tree(node, out):
    nm = node.get("/Names")
    if nm is not None:
        items = _deref(nm)
        for i in range(0, len(items), 2):
            v = _deref(items[i + 1])
            if isinstance(v, dict) and "/D" in v:
                v = _deref(v["/D"])
            out[str(items[i])] = v
    kids = node.get("/Kids")
    if kids is not None:
        for kid in _deref(kids):
            _walk_name_tree(_deref(kid), out)


def _collect_named_dests(reader):
    """Return `{name: dest_array}` for the document. Handles both the
    legacy `/Dests` dict on the catalog and the `/Names → /Dests`
    name tree used by modern PDFs."""
    out = {}
    catalog = reader.trailer["/Root"]
    flat = _deref(catalog.get("/Dests"))
    if flat:
        for k, v in flat.items():
            v = _deref(v)
            if isinstance(v, dict) and "/D" in v:
                v = _deref(v["/D"])
            out[str(k)] = v
    names = _deref(catalog.get("/Names"))
    if names:
        d = _deref(names.get("/Dests"))
        if d:
            _walk_name_tree(d, out)
    return out


def _page_indirect_to_index(reader, page_obj):
    if not isinstance(page_obj, IndirectObject):
        return None
    target = page_obj.idnum
    for i, p in enumerate(reader.pages):
        ref = p.indirect_reference
        if ref is not None and ref.idnum == target:
            return i
    return None


def _dest_top(resolved):
    """Pull the `top` coordinate out of a destination array.

    Destination arrays look like `[page, fit_mode, ...args]`. The
    coordinate of interest depends on fit mode:
      /XYZ left top zoom    -> args[1]   (top is 2nd arg)
      /FitH top             -> args[0]
      /FitBH top            -> args[0]
    For other fit modes (Fit, FitV, FitR, FitB, FitBV) there's no
    explicit top, so we return None and the viewer falls back to
    scrolling to the page's top edge."""
    if not isinstance(resolved, (list, ArrayObject)) or len(resolved) < 2:
        return None
    fit = str(resolved[1])
    if fit == "/XYZ" and len(resolved) >= 4 and resolved[3] is not None:
        try:
            return float(resolved[3])
        except (TypeError, ValueError):
            return None
    if fit in ("/FitH", "/FitBH") and len(resolved) >= 3 and resolved[2] is not None:
        try:
            return float(resolved[2])
        except (TypeError, ValueError):
            return None
    return None


def read_citation_links(pdf_path):
    """Return `{page_idx: [(rect, target_page, target_top, ref_n), ...]}`.

    `rect` is `(x1, y1, x2, y2)` in PDF user space (origin at the
    bottom-left, y growing upward). `target_page` is 0-based.
    `target_top` is the y-coordinate to scroll to (PDF user space),
    or None if the destination's fit mode doesn't specify one.
    `ref_n` is the reference number extracted from a Springer-style
    `:CR<N>:` named destination, or None when the dest doesn't use
    that naming scheme (the link still works, we just can't tell
    which entry it points to from the name alone)."""
    if not _HAVE_PYPDF:
        return {}
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return {}

    dests = _collect_named_dests(reader)
    out = {}
    for pi, page in enumerate(reader.pages):
        annots = _deref(page.get("/Annots"))
        if not isinstance(annots, (list, ArrayObject)):
            continue
        page_links = []
        for a in annots:
            a = _deref(a)
            if a.get("/Subtype") != "/Link":
                continue
            rect = a.get("/Rect")
            if not rect or len(rect) != 4:
                continue
            try:
                rect_t = (float(rect[0]), float(rect[1]),
                          float(rect[2]), float(rect[3]))
            except (TypeError, ValueError):
                continue

            # The destination can be either a direct /Dest on the
            # annotation or an /A action with /S=/GoTo and /D=dest.
            d = a.get("/Dest")
            if d is None:
                action = _deref(a.get("/A"))
                if action is None or action.get("/S") != "/GoTo":
                    continue
                d = _deref(action.get("/D"))
            else:
                d = _deref(d)

            if isinstance(d, str):
                resolved = dests.get(d)
                m = _CR_RE.search(d)
                ref_n = int(m.group(1)) if m else None
            else:
                resolved = d
                ref_n = None
            if not isinstance(resolved, (list, ArrayObject)) or len(resolved) < 1:
                continue

            tgt_page = _page_indirect_to_index(reader, resolved[0])
            if tgt_page is None:
                continue
            top = _dest_top(resolved)
            page_links.append((rect_t, tgt_page, top, ref_n))
        if page_links:
            out[pi] = page_links
    return out


def assign_ref_n_by_position(citation_links, bibliography_positions,
                             tolerance=12.0):
    """Patch a `citation_links` dict (as returned by
    `read_citation_links`) so Links whose destination name didn't
    match the `CR<N>` pattern (`ref_n is None`) get a reference
    number from the parsed bibliography by y-coordinate matching.

    `bibliography_positions` is `[(n, page_idx, y_pdf), ...]` in
    PDF user space — exactly what
    `references_pdf.bibliography_positions(pdf_path)` returns.

    For each `ref_n=None` Link whose `(target_page, target_top)`
    falls within `tolerance` PDF-points of a parsed entry's
    `(page, y)`, we assign the entry's `n`. Links that don't match
    (e.g. figure / section cross-references) are left untouched —
    they keep working as plain jumps with no popover.

    Returns a new dict; the input is not mutated."""
    if not bibliography_positions:
        return citation_links
    by_page = {}
    for n, page, y in bibliography_positions:
        by_page.setdefault(page, []).append((y, n))

    out = {}
    for page_idx, links in citation_links.items():
        new_links = []
        for rect, target_page, target_top, ref_n in links:
            if (ref_n is None
                    and target_top is not None
                    and target_page in by_page):
                best_n = None
                best_dy = None
                for y, n in by_page[target_page]:
                    dy = abs(target_top - y)
                    if best_dy is None or dy < best_dy:
                        best_dy = dy
                        best_n = n
                if best_dy is not None and best_dy <= tolerance:
                    ref_n = best_n
            new_links.append((rect, target_page, target_top, ref_n))
        out[page_idx] = new_links
    return out
