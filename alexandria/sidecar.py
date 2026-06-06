"""Per-PDF sidecar metadata file (the canonical store).

Lives next to the PDF as <pdf>.alexandria. Plain JSON (the
extension is a UX signal, not a format change — the file is JSON
and any JSON tool will read it), hand-editable, survives any DB
schema change. Pre-v0.1.0 sidecars used the `.meta.json` suffix
and are migrated in-place on startup by `migrate_library_sidecars`.
"""

import json
import os
import socket
from datetime import date


def _tmp_suffix():
    """Return `.<host>.<pid>.tmp` for the current process. Two
    Alexandria processes on different hosts writing the same
    sidecar on an NFS share would otherwise both write to
    `<path>.tmp` and race the truncate/write/rename triple — one
    would see a corrupt tmp mid-flush. Per-host, per-pid suffix
    eliminates that. Doesn't fix the last-rename-wins race (a
    separate BACKLOG item plans an mtime check before rename)."""
    host = (socket.gethostname() or "host").split(".", 1)[0]
    # Restrict to a conservative charset — sidecars live on disk
    # and we'd rather not embed weird hostnames in filenames.
    host = "".join(c if (c.isalnum() or c in "-_") else "_"
                   for c in host) or "host"
    return ".{}.{}.tmp".format(host, os.getpid())

SCHEMA_VERSION = 1
SIDECAR_SUFFIX = ".alexandria"
# Historical suffix; only used by the one-shot migration walker
# (migrate_library_sidecars) on startup. New writes never use this.
LEGACY_SIDECAR_SUFFIX = ".meta.json"

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
    if not pdf_path:
        return False
    # Older rows occasionally stored pdf_path as bytes (BLOB affinity)
    # via a path-encoding round-trip; tolerate either type here so the
    # whole catalogue doesn't fail to load on a single bad row.
    if isinstance(pdf_path, (bytes, bytearray)):
        return bytes(pdf_path).startswith(GHOST_PATH_PREFIX.encode("utf-8"))
    return pdf_path.startswith(GHOST_PATH_PREFIX)


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
    tmp = path + _tmp_suffix()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def migrate_library_sidecars(library_root):
    """One-shot rename of every legacy ``*.meta.json`` sidecar to
    ``*.alexandria``. Idempotent: a second invocation is a no-op
    because the suffix is no longer found.

    Walks the library root recursively so the ``.alexandria-bibtex``
    ghost subdirectory is covered without a special case. Skips any
    legacy file whose target name already exists (extremely
    unlikely, but safer than silently clobbering hand-edited
    content).

    Renames sidecars only — the SQLite cache's stale
    ``sidecar_path`` columns get refreshed on the next watcher
    event / startup walk. Logged to stderr so a user who runs
    Alexandria from a terminal sees the migration trail.

    Returns the number of files renamed."""
    if not library_root or not os.path.isdir(library_root):
        return 0
    renamed = 0
    for dirpath, _dirs, files in os.walk(library_root):
        for name in files:
            if not name.endswith(LEGACY_SIDECAR_SUFFIX):
                continue
            src = os.path.join(dirpath, name)
            dst = src[:-len(LEGACY_SIDECAR_SUFFIX)] + SIDECAR_SUFFIX
            if os.path.exists(dst):
                continue
            try:
                os.replace(src, dst)
                renamed += 1
            except OSError as e:
                import sys
                print("sidecar migration: {} → {} failed: {}".format(
                    src, dst, e), file=sys.stderr)
    if renamed:
        import sys
        print("sidecar migration: renamed {} legacy *{} → *{}"
              .format(renamed, LEGACY_SIDECAR_SUFFIX, SIDECAR_SUFFIX),
              file=sys.stderr)
    return renamed
