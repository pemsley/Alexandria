"""Per-PDF sidecar metadata file (the canonical store).

Lives next to the PDF as <pdf>.meta.json. Plain JSON, hand-editable,
survives any DB schema change.
"""

import json
import os
from datetime import date

SCHEMA_VERSION = 1
SIDECAR_SUFFIX = ".meta.json"

# Ghost (PDF-less) entries imported from BibTeX live in this hidden
# subdirectory of LIBRARY_ROOT. Their `pdf_path` in the index is
# `bibtex:<key>` — a synthetic identifier, not a filesystem path.
GHOST_SUBDIR = ".alexandria-bibtex"
GHOST_PATH_PREFIX = "bibtex:"


def sidecar_path_for(pdf_path):
    return pdf_path + SIDECAR_SUFFIX


def thumb_path_for(pdf_path):
    return pdf_path + ".thumb.png"


def is_ghost_path(pdf_path):
    """True for synthetic `bibtex:<key>` identifiers used by BibTeX-only
    library entries (no PDF on disk)."""
    return bool(pdf_path) and pdf_path.startswith(GHOST_PATH_PREFIX)


def ghost_pdf_path(bibtex_key):
    return GHOST_PATH_PREFIX + bibtex_key


def ghost_sidecar_path(library_root, bibtex_key):
    """Where the ghost sidecar JSON lives on disk."""
    return os.path.join(library_root, GHOST_SUBDIR,
                        bibtex_key + SIDECAR_SUFFIX)


def new_record(pdf_path):
    return {
        "schema": SCHEMA_VERSION,
        "pdf_filename": os.path.basename(pdf_path),
        "title": None,
        "authors": [],
        "year": None,
        "doi": None,
        "journal": None,
        "tags": [],
        "notes": "",
        "mark": None,           # "red" / "orange" / "green" / "cyan" / None
        "hand_edited": False,
        "added_date": date.today().isoformat(),
        "sha256": None,
        "citations": None,
        "citations_source": None,
        "citations_fetched": None,
        # OpenAlex per-year breakdown: list of {year, count}, oldest first.
        "citations_by_year": [],
        "auto_keywords": [],
        "abstract": None,
        # Rich author info from OpenAlex: list of {name, position,
        # orcid, openalex_id, institution} dicts. The flat 'authors'
        # list above is kept in sync (display names, in publication
        # order) for back-compat and display.
        "authorships": [],
        # If this PDF is a preprint and OpenAlex knows of a journal-
        # published version: {doi, title, journal, year, openalex_id,
        # checked}. None for non-preprints or when no match was found.
        "published_version": None,
        # User highlights / comments from the built-in viewer. Each entry:
        #   {"id": uuid, "page": int (0-based),
        #    "quads": [[x, y, w, h], ...]   (PDF points, y-down-from-top),
        #    "text": str, "color": str,
        #    "comment": str, "author": str,
        #    "created": iso8601, "modified": iso8601}
        "highlights": [],
        # BibTeX provenance / round-trip support. When the entry came
        # from a `.bib` import these get populated; otherwise they're
        # quietly None / {}. `bibtex_extra` carries fields we don't
        # promote to the top level (volume, number, pages, publisher,
        # url, abstract, keywords, ...) so re-export is faithful.
        "bibtex_key": None,
        "bibtex_type": None,
        "bibtex_extra": {},
        # Cached OpenAlex popover lists (avoid re-querying on every
        # popover open). Keys are absent on legacy sidecars; readers
        # should use `.get()`. Schema:
        #   cited_by_cache:    {recent: [...], cited: [...], fetched: iso8601}
        #   references_cache:  {refs: [...], refs_pdf: [...], source: str,
        #                       fetched: iso8601}
        # The cached items are full work-dicts (the same shape that
        # metrics.fetch_cited_by / fetch_references return), so the
        # popover can render straight from them.
        "cited_by_cache": None,
        "references_cache": None,
        "raw": {},
    }


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write(path, record):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
