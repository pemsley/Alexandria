"""Import a `.bib` file into the Alexandria library.

Each entry becomes either:

* a normal library row (when the entry has a `file = {...}` field
  pointing at a PDF on disk) — the PDF goes through the standard
  importer.import_pdf path, then the resulting sidecar is patched
  with `bibtex_key`, `bibtex_type` and `bibtex_extra`.

* a *ghost* row (when there's no usable file) — a sidecar file is
  created in `LIBRARY_ROOT/.alexandria-bibtex/<key>.alexandria`, the
  index row's `pdf_path` is the synthetic `bibtex:<key>`, and there
  is no PDF / thumbnail. The card UI shows it without Open / Rename
  buttons and offers a "Get PDF" action instead.

Duplicate handling: if an entry's DOI matches an existing library
row, we don't create a new entry — but we do patch the BibTeX
provenance (key / type / extra) onto the existing sidecar so future
re-export keeps the citation key intact.

OpenAlex enrichment runs for every entry that has a DOI.
"""

import os
import re
import shutil

from . import sidecar, importer, index, metrics, extract


def _enrich_with_openalex(rec):
    """Best-effort OpenAlex enrichment by DOI, in place."""
    doi = rec.get("doi")
    if not doi:
        return
    (n, src, kw, abstract, authorships, cby,
     oa_title, oa_year, is_oa, oa_status,
     funders, grants) = metrics.fetch_metrics(doi)
    # Cross-contamination check — see importer._openalex_record_matches.
    from . import importer as _importer
    if not _importer._openalex_record_matches(
            rec.get("title"), rec.get("year"), oa_title, oa_year):
        if oa_title or oa_year:
            print("[bibtex_import] OpenAlex record for {} looks "
                  "corrupted — keeping BibTeX-supplied metadata".format(doi))
        return
    if n is not None:
        rec["citations"] = n
        rec["citations_source"] = src
        rec["citations_fetched"] = metrics.today_iso()
    if kw:
        rec["auto_keywords"] = kw
    if abstract and not rec.get("abstract"):
        rec["abstract"] = abstract
    if authorships:
        rec["authorships"] = authorships
        oa_names = [a["name"] for a in authorships if a.get("name")]
        if oa_names:
            rec["authors"] = oa_names
    if cby:
        rec["citations_by_year"] = cby
    if is_oa is not None:
        rec["is_oa"] = is_oa
    if oa_status:
        rec["oa_status"] = oa_status
    if funders:
        rec["funders"] = funders
    if grants:
        rec["grants"] = grants


def _apply_bibtex_provenance(rec, br):
    """Copy the BibTeX-specific fields from a `bibtex.parse` record
    onto a sidecar record, in place."""
    if br.get("bibtex_key"):
        rec["bibtex_key"] = br["bibtex_key"]
    if br.get("bibtex_type"):
        rec["bibtex_type"] = br["bibtex_type"]
    extra = br.get("bibtex_extra") or {}
    if extra:
        rec["bibtex_extra"] = dict(extra)


def _import_with_pdf(conn, br, file_path):
    """An entry whose `file = {...}` resolves to a real PDF on disk.
    Run the normal PDF import flow, then patch the sidecar to record
    the BibTeX provenance."""
    rec, status = importer.import_pdf(conn, file_path)
    if rec is None:
        return None, status
    sc_path = sidecar.sidecar_path_for(file_path)
    try:
        cur = sidecar.read(sc_path)
    except Exception:
        return rec, status
    # If the user already had this PDF imported with no BibTeX
    # provenance, fill it in. Don't clobber existing values.
    if not cur.get("bibtex_key"):
        _apply_bibtex_provenance(cur, br)
        try:
            sidecar.write(sc_path, cur)
            mtime = os.path.getmtime(sc_path)
            th = sidecar.thumb_path_for(file_path)
            index.upsert(
                conn, file_path, sc_path,
                th if os.path.isfile(th) else None, cur, mtime)
        except Exception as e:
            print("bibtex_import: sidecar patch failed for "
                  "{}: {}".format(sc_path, e))
    return cur, status


def _create_ghost(conn, br, library_root):
    """Create a PDF-less sidecar for a BibTeX entry. The sidecar lives
    in `LIBRARY_ROOT/.alexandria-bibtex/<key>.alexandria` and the
    index row uses `pdf_path = bibtex:<key>`."""
    key = br.get("bibtex_key")
    if not key:
        return None, "error"

    sc_path = sidecar.ghost_sidecar_path(library_root, key)
    pdf_path = sidecar.ghost_pdf_path(key)

    rec = sidecar.new_record(pdf_path)
    rec["pdf_filename"] = ""        # no real file
    rec["title"] = br.get("title")
    rec["authors"] = list(br.get("authors") or [])
    rec["year"] = br.get("year")
    rec["journal"] = br.get("journal")
    rec["doi"] = br.get("doi")
    _apply_bibtex_provenance(rec, br)

    _enrich_with_openalex(rec)

    os.makedirs(os.path.dirname(sc_path), exist_ok=True)
    sidecar.write(sc_path, rec)
    mtime = os.path.getmtime(sc_path)
    index.upsert(conn, pdf_path, sc_path, None, rec, mtime)
    return rec, "ghost"


def _patch_existing(conn, dup_row, br):
    """Existing library row whose DOI matches the BibTeX entry. Add
    the BibTeX provenance fields if they aren't already there."""
    sc_path = dup_row.get("sidecar_path")
    if not sc_path or not os.path.isfile(sc_path):
        return None, "duplicate"
    try:
        rec = sidecar.read(sc_path)
    except Exception:
        return None, "duplicate"
    changed = False
    if not rec.get("bibtex_key") and br.get("bibtex_key"):
        rec["bibtex_key"] = br["bibtex_key"]
        changed = True
    if not rec.get("bibtex_type") and br.get("bibtex_type"):
        rec["bibtex_type"] = br["bibtex_type"]
        changed = True
    if not rec.get("bibtex_extra") and br.get("bibtex_extra"):
        rec["bibtex_extra"] = dict(br["bibtex_extra"])
        changed = True
    if changed:
        try:
            sidecar.write(sc_path, rec)
            mtime = os.path.getmtime(sc_path)
            th = dup_row.get("thumb_path")
            index.upsert(
                conn, dup_row["pdf_path"], sc_path,
                th if (th and os.path.isfile(th)) else None,
                rec, mtime)
        except Exception as e:
            print("bibtex_import: dup-patch failed for "
                  "{}: {}".format(sc_path, e))
    return rec, "duplicate"


def import_record(conn, br, library_root):
    """Import a single BibTeX record dict (as returned by
    `bibtex.parse`). Returns (rec, status) where status is one of
    'imported', 'ghost', 'duplicate', or 'error'."""
    # Already in the library by DOI?
    doi = br.get("doi")
    if doi:
        dup = index.find_duplicate(conn, doi=doi, exclude_path="")
        if dup:
            return _patch_existing(conn, dup, br)

    # Real PDF on disk?
    file_path = br.get("file")
    if file_path and os.path.isabs(file_path) and os.path.isfile(file_path):
        return _import_with_pdf(conn, br, file_path)

    # Otherwise: ghost.
    return _create_ghost(conn, br, library_root)


# ----------------------------------------------------------------------
# Attaching a PDF to an existing ghost (BibTeX-only) entry.
# ----------------------------------------------------------------------

# Bibtex keys can contain `:` and other characters that are awkward in
# filenames; sanitise to a portable subset.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _filename_for_key(bibtex_key):
    safe = _FILENAME_SAFE_RE.sub("_", bibtex_key).strip("_")
    return safe or "ghost"


def _unique_target_path(library_root, bibtex_key):
    """Pick a non-colliding `<library>/<key>.pdf`. Suffix `-1`, `-2`,
    ... if the key collides with an existing file."""
    base = _filename_for_key(bibtex_key)
    candidate = os.path.join(library_root, base + ".pdf")
    if not os.path.exists(candidate):
        return candidate
    n = 1
    while True:
        candidate = os.path.join(
            library_root, "{}-{}.pdf".format(base, n))
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _normalised_doi(doi):
    if not doi:
        return ""
    return index.normalize_doi(doi).lower() if index.normalize_doi(doi) else ""


def _ghost_doi_check(ghost_doi, src_pdf_path):
    """Compare the ghost's DOI to whatever DOI we can scrape out of
    the source PDF. Returns (ok, message). If both have a DOI and
    they disagree, ok is False — the merge should be rejected."""
    g = _normalised_doi(ghost_doi)
    if not g:
        return True, ""              # ghost has no DOI, can't check.
    try:
        src_doi = extract._scan_doi_in_pages(src_pdf_path, max_pages=4)
    except Exception:
        src_doi = None
    s = _normalised_doi(src_doi)
    if not s:
        return True, ""              # PDF has no DOI we can find.
    if g != s:
        return False, ("PDF rejected: its DOI {} doesn't match the "
                       "BibTeX entry's {}".format(src_doi, ghost_doi))
    return True, ""


def attach_pdf_to_ghost(conn, ghost_row, source_pdf_path, library_root):
    """Attach `source_pdf_path` to the ghost (BibTeX-only) entry
    represented by `ghost_row` (an index row dict, must be a ghost).

    Steps:
      1. DOI gate: if ghost and PDF both have a DOI and they disagree,
         reject.
      2. Copy the PDF into LIBRARY_ROOT as `<bibtex_key>.pdf` (with
         numeric suffix on collision).
      3. Run the standard PDF import (pdfx + OpenAlex enrichment).
      4. Merge ghost's curation onto the new sidecar:
         - always:  bibtex_key, bibtex_type, bibtex_extra, mark
         - if non-empty: notes, tags, highlights, published_version
         - if ghost.hand_edited: also title/authors/year/journal/doi,
           and set hand_edited=True on the new entry so future
           refreshes won't clobber the user's manual fixes.
      5. Re-write the new sidecar; re-upsert the index row.
      6. Delete the ghost: remove its sidecar file from
         `<library>/.alexandria-bibtex/`, and remove its index row.

    Returns `(new_pdf_path, status, message)` where `status` is one
    of: 'merged', 'doi_mismatch', 'not_a_ghost', 'error'."""
    ghost_pdf_path = ghost_row.get("pdf_path") if hasattr(
        ghost_row, "get") else ghost_row["pdf_path"]
    if not sidecar.is_ghost_path(ghost_pdf_path):
        return None, "not_a_ghost", "Target is not a BibTeX-only entry"

    if not source_pdf_path or not os.path.isfile(source_pdf_path):
        return None, "error", "Dropped file is missing or unreadable"
    if not source_pdf_path.lower().endswith(".pdf"):
        return None, "error", "Dropped file is not a .pdf"

    # Read the ghost sidecar to get all curation fields.
    ghost_sc = ghost_row.get("sidecar_path") if hasattr(
        ghost_row, "get") else ghost_row["sidecar_path"]
    try:
        ghost_rec = sidecar.read(ghost_sc)
    except Exception as e:
        return None, "error", "Cannot read ghost sidecar: {}".format(e)

    # DOI gate.
    ok, msg = _ghost_doi_check(ghost_rec.get("doi"), source_pdf_path)
    if not ok:
        return None, "doi_mismatch", msg

    bibtex_key = ghost_rec.get("bibtex_key") or "ghost"
    target_path = _unique_target_path(library_root, bibtex_key)

    # Drop the ghost FIRST. Otherwise importer.import_pdf below will
    # match the new file's DOI against the ghost and return "duplicate"
    # — exactly what we don't want. Snapshot ghost_rec is held in
    # memory; we restore it if anything below fails.
    try:
        importer.delete_pdf(conn, ghost_pdf_path)
    except Exception as e:
        return None, "error", "Could not remove ghost: {}".format(e)

    def _restore_ghost():
        try:
            sc = sidecar.ghost_sidecar_path(library_root, bibtex_key)
            sidecar.write(sc, ghost_rec)
            mtime = os.path.getmtime(sc)
            index.upsert(conn, ghost_pdf_path, sc, None, ghost_rec, mtime)
        except Exception as e:
            print("attach_pdf_to_ghost: ghost restore failed: {}".format(e))

    # Copy the PDF in.
    try:
        os.makedirs(library_root, exist_ok=True)
        shutil.copy2(source_pdf_path, target_path)
    except OSError as e:
        _restore_ghost()
        return None, "error", "Could not copy PDF: {}".format(e)

    # Standard import flow runs pdfx + OpenAlex enrichment.
    try:
        new_rec, status = importer.import_pdf(conn, target_path)
    except Exception as e:
        try:
            os.remove(target_path)
        except OSError:
            pass
        _restore_ghost()
        return None, "error", "import_pdf failed: {}".format(e)
    if new_rec is None or status in ("duplicate",):
        try:
            os.remove(target_path)
        except OSError:
            pass
        _restore_ghost()
        return None, "error", "import_pdf returned status={}".format(status)

    # Merge ghost curation onto the new sidecar.
    new_sc = sidecar.sidecar_path_for(target_path)
    try:
        cur = sidecar.read(new_sc)
    except Exception:
        cur = new_rec

    # Always-merged: BibTeX provenance + mark.
    if ghost_rec.get("bibtex_key"):
        cur["bibtex_key"] = ghost_rec["bibtex_key"]
    if ghost_rec.get("bibtex_type"):
        cur["bibtex_type"] = ghost_rec["bibtex_type"]
    if ghost_rec.get("bibtex_extra"):
        cur["bibtex_extra"] = dict(ghost_rec["bibtex_extra"])
    if ghost_rec.get("mark"):
        cur["mark"] = ghost_rec["mark"]

    # If non-empty: user data we don't want to lose.
    for key in ("notes", "tags", "highlights", "published_version"):
        gv = ghost_rec.get(key)
        if gv:
            cur[key] = gv

    # hand_edited rule: if the ghost was hand_edited, the user
    # already curated its bibliographic fields — those win, and
    # hand_edited stays True so future refreshes preserve them.
    if ghost_rec.get("hand_edited"):
        for key in ("title", "authors", "year", "journal", "doi"):
            v = ghost_rec.get(key)
            if v:
                cur[key] = v
        cur["hand_edited"] = True

    try:
        sidecar.write(new_sc, cur)
        mtime = os.path.getmtime(new_sc)
        thumb = sidecar.thumb_path_for(target_path)
        index.upsert(
            conn, target_path, new_sc,
            thumb if os.path.isfile(thumb) else None, cur, mtime)
    except Exception as e:
        return target_path, "error", \
            "merge succeeded but sidecar write failed: {}".format(e)

    # Ghost was already removed above (before import_pdf, to avoid the
    # DOI-collision-with-self problem). Nothing more to do.
    return target_path, "merged", \
        "Attached PDF to «{}»".format(bibtex_key)


def import_bib(conn, bib_path, library_root, on_progress=None):
    """Parse a `.bib` file and import every entry. Returns a counts
    dict: `{imported, ghost, duplicate, error}`. `on_progress(i, n,
    key, status)` is called once per entry if supplied."""
    from . import bibtex
    records = bibtex.parse(bib_path)
    n = len(records)
    counts = {"imported": 0, "ghost": 0, "duplicate": 0, "error": 0}
    for i, br in enumerate(records):
        try:
            _rec, status = import_record(conn, br, library_root)
        except Exception as e:
            print("bibtex_import: {} failed: {}".format(
                br.get("bibtex_key"), e))
            status = "error"
        if status not in counts:
            status = "error"
        counts[status] += 1
        if on_progress:
            on_progress(i + 1, n, br.get("bibtex_key"), status)
    return counts
