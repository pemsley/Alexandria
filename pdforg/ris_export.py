"""Export sidecar records to RIS.

RIS is the line-oriented citation interchange format that EndNote,
RefWorks, Mendeley, Zotero and most journal "Cite this paper as…"
widgets understand natively. The wire format is one `TAG  - value`
line per field, records bracketed by `TY  - <type>` … `ER  - `.

Mirrors `bibtex_export`: a data-layer helper
(`sidecar_to_ris_lines`) that turns one sidecar dict into a list of
`(tag, value)` tuples, and `export_rows_to_file(rows, path)` for
the GUI's "Export RIS…" command.
"""

import os
import re

from . import sidecar


# BibTeX entry type → RIS TY tag. Anything we don't recognise falls
# back to JOUR-if-journal-known else GEN.
_TY_FROM_BIBTEX = {
    "article":         "JOUR",
    "inproceedings":   "CONF",
    "conference":      "CONF",
    "incollection":    "CHAP",
    "inbook":          "CHAP",
    "book":            "BOOK",
    "phdthesis":       "THES",
    "mastersthesis":   "THES",
    "techreport":      "RPRT",
    "manual":          "GEN",
    "misc":            "GEN",
    "unpublished":     "UNPB",
}


def _ty_for(rec):
    bt = (rec.get("bibtex_type") or "").strip().lower()
    if bt in _TY_FROM_BIBTEX:
        return _TY_FROM_BIBTEX[bt]
    return "JOUR" if rec.get("journal") else "GEN"


def _to_ris_name(name):
    """Display-form ('John Smith') → RIS 'Last, First'.

    If the name already contains a comma, it's assumed to be in
    'Last, First' form and passed through unchanged. Otherwise we
    take the last whitespace-separated token as the surname — same
    heuristic as `bibtex_export._surname`. Misorders compound
    surnames like 'van der Waals'; that's a known limitation
    documented in the BibTeX layer too."""
    if not name:
        return ""
    name = name.strip()
    if "," in name:
        return name
    parts = name.split()
    if len(parts) <= 1:
        return name
    surname = parts[-1]
    given = " ".join(parts[:-1])
    return "{}, {}".format(surname, given)


_PAGES_RE = re.compile(r"^\s*(\S+?)\s*[-–—]\s*(\S+)\s*$")


def _split_pages(pages):
    """Split a 'pages' string like '123-130' (or '123–130') into
    `(start, end)`. A single page or unparseable value returns
    `(value, None)`."""
    if not pages:
        return None, None
    s = str(pages).strip()
    if not s:
        return None, None
    m = _PAGES_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    return s, None


def sidecar_to_ris_lines(rec, pdf_path=None):
    """Convert a sidecar dict to a list of (TAG, value) 2-tuples
    ready to be serialised by `lines_to_text`.

    Multi-valued fields (`AU`, `KW`) appear multiple times in the
    list, which matches RIS's wire format (each `KW` is a separate
    line)."""
    lines = []
    lines.append(("TY", _ty_for(rec)))

    for a in (rec.get("authors") or []):
        if a:
            lines.append(("AU", _to_ris_name(a)))

    if rec.get("title"):
        lines.append(("TI", rec["title"]))

    if rec.get("year") is not None:
        lines.append(("PY", str(rec["year"])))

    # Journal: T2 is the standard "secondary title" tag that Zotero,
    # Mendeley and Papers all read as "container title" — works for
    # both journal articles (journal name) and book chapters (book
    # title), so we use it uniformly.
    if rec.get("journal"):
        lines.append(("T2", rec["journal"]))

    if rec.get("doi"):
        lines.append(("DO", rec["doi"]))

    extra = rec.get("bibtex_extra") or {}
    if extra.get("volume"):
        lines.append(("VL", str(extra["volume"])))
    issue = extra.get("number") or extra.get("issue")
    if issue:
        lines.append(("IS", str(issue)))
    sp, ep = _split_pages(extra.get("pages"))
    if sp:
        lines.append(("SP", sp))
    if ep:
        lines.append(("EP", ep))
    if extra.get("publisher"):
        lines.append(("PB", str(extra["publisher"])))
    if extra.get("url"):
        lines.append(("UR", str(extra["url"])))

    abs_text = rec.get("abstract") or extra.get("abstract")
    if abs_text:
        lines.append(("AB", abs_text))

    # Keywords: prefer the BibTeX-extra "keywords" string (comma- or
    # semicolon-separated, reflects what was imported); fall back to
    # the OpenAlex auto_keywords list. One KW tag per term.
    kw_field = extra.get("keywords")
    if isinstance(kw_field, str):
        for kw in re.split(r"[,;]", kw_field):
            kw = kw.strip()
            if kw:
                lines.append(("KW", kw))
    elif isinstance(kw_field, list):
        for kw in kw_field:
            if kw:
                lines.append(("KW", str(kw).strip()))
    else:
        for kw in (rec.get("auto_keywords") or []):
            if kw:
                lines.append(("KW", str(kw).strip()))

    if pdf_path and not sidecar.is_ghost_path(pdf_path):
        lines.append(("L1", "file://" + pdf_path))

    lines.append(("ER", ""))
    return lines


def lines_to_text(lines):
    """Render one record's `(tag, value)` list to RIS text. RIS is
    line-oriented and doesn't allow embedded newlines in values, so
    we collapse them to spaces."""
    out = []
    for tag, value in lines:
        v = (value or "")
        if not isinstance(v, str):
            v = str(v)
        v = v.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        out.append("{}  - {}".format(tag, v))
    return "\n".join(out)


def records_to_text(records):
    """Concatenate multiple records (each a `lines` list) into one
    RIS file. Records are separated by a blank line; the file ends
    with a single trailing newline."""
    parts = [lines_to_text(r) for r in records]
    return "\n\n".join(parts) + ("\n" if parts else "")


def export_rows_to_file(rows, output_path):
    """Write a `.ris` file from a list of index rows. Each row must
    expose `pdf_path` and `sidecar_path` (sqlite3.Row works). The
    sidecar — the canonical store — is read for each row.

    Returns `(written, skipped)`."""
    records = []
    skipped = 0
    for row in rows:
        try:
            sc_path = row["sidecar_path"]
        except (KeyError, IndexError, TypeError):
            sc_path = None
        if not sc_path or not os.path.isfile(sc_path):
            skipped += 1
            continue
        try:
            rec = sidecar.read(sc_path)
        except Exception as e:
            print("ris_export: cannot read {}: {}".format(sc_path, e))
            skipped += 1
            continue
        try:
            pdf_path = row["pdf_path"]
        except (KeyError, IndexError, TypeError):
            pdf_path = None
        records.append(sidecar_to_ris_lines(rec, pdf_path))

    text = records_to_text(records)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(records), skipped
