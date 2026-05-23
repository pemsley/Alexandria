"""Export sidecar records to BibTeX.

The data layer is `sidecar_to_bibtex_record(rec, pdf_path)` which
turns a sidecar dict into a bibtex.parse-style dict ready for
`bibtex.write([...])`. The convenience entry point
`export_rows_to_file(rows, path)` reads each row's sidecar from
disk (the canonical store), de-duplicates citation keys, and writes
a `.bib` file.
"""

import os
import re

from . import bibtex, sidecar


# Words skipped when synthesising a key from the title — keeps the
# auto-generated key meaningful (smith2010features rather than
# smith2010the).
_KEY_TITLE_STOPWORDS = frozenset((
    "a", "an", "the",
    "of", "in", "on", "at", "by", "to", "from", "for", "with",
    "and", "or", "but", "as",
    "this", "these", "that", "those", "is", "are",
))


def _surname(name):
    """Best-effort surname from a display-form name. Same heuristic
    used elsewhere: last whitespace-separated token. Misorders compound
    surnames like 'van der Waals'."""
    if not name:
        return ""
    parts = name.strip().split()
    return parts[-1] if parts else ""


def _autogenerate_key(rec):
    """Build a citation key from author surname + year + first
    significant title word, e.g. 'emsley2010features'."""
    authors = rec.get("authors") or []
    surname = _surname(authors[0]) if authors else "anon"
    surname = re.sub(r"[^A-Za-z0-9]", "", surname).lower() or "anon"
    year = rec.get("year")
    year_part = str(year) if year else "nodate"
    title_word = ""
    for w in (rec.get("title") or "").split():
        plain = re.sub(r"[^A-Za-z0-9]", "", w).lower()
        if plain and plain not in _KEY_TITLE_STOPWORDS:
            title_word = plain[:14]
            break
    parts = [surname, year_part]
    if title_word:
        parts.append(title_word)
    return "".join(parts)


def _default_type(rec):
    """Pick a reasonable BibTeX entry type when the record didn't
    come from BibTeX in the first place."""
    if rec.get("journal"):
        return "article"
    return "misc"


def sidecar_to_bibtex_record(rec, pdf_path=None):
    """Convert a sidecar dict to a bibtex.parse-style record dict
    that's ready to feed to `bibtex.write([...])`.

    `pdf_path` is the on-disk PDF path; when set and not a synthetic
    `bibtex:<key>` ghost path, it's emitted as a `file = {...}`
    field so the receiving side can re-link the PDF on import."""
    key = (rec.get("bibtex_key") or "").strip() or _autogenerate_key(rec)
    btype = (rec.get("bibtex_type") or "").strip() or _default_type(rec)

    file_field = None
    if pdf_path and not sidecar.is_ghost_path(pdf_path):
        file_field = pdf_path

    extra = dict(rec.get("bibtex_extra") or {})

    return {
        "bibtex_key": key,
        "bibtex_type": btype,
        "title": rec.get("title"),
        "authors": list(rec.get("authors") or []),
        "year": rec.get("year"),
        "journal": rec.get("journal"),
        "doi": rec.get("doi"),
        "file": file_field,
        "bibtex_extra": extra,
    }


def _dedup_key(key, seen):
    """Append a numeric suffix when a key is already used. The
    original key is preferred for the *first* occurrence."""
    if key not in seen:
        return key
    n = 2
    while True:
        candidate = "{}_{}".format(key, n)
        if candidate not in seen:
            return candidate
        n += 1


def records_to_text(records):
    """Render a list of bibtex-record dicts to a `.bib` string. Keys
    are de-duplicated; the rest is delegated to bibtex.write()."""
    seen = set()
    deduped = []
    for r in records:
        new_key = _dedup_key(r["bibtex_key"], seen)
        seen.add(new_key)
        if new_key != r["bibtex_key"]:
            r = dict(r)
            r["bibtex_key"] = new_key
        deduped.append(r)
    return bibtex.write(deduped)


def export_rows_to_file(rows, output_path):
    """Write a `.bib` file from a list of index rows. Each row must
    expose `pdf_path` and `sidecar_path` (sqlite3.Row works). The
    sidecar — the canonical store — is read for each row. Returns
    `(written, skipped)`."""
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
            print("bibtex_export: cannot read {}: {}".format(sc_path, e))
            skipped += 1
            continue
        try:
            pdf_path = row["pdf_path"]
        except (KeyError, IndexError, TypeError):
            pdf_path = None
        records.append(sidecar_to_bibtex_record(rec, pdf_path))

    text = records_to_text(records)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(records), skipped
