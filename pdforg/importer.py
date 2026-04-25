"""Walk a directory tree, ensure each PDF has a sidecar + thumbnail,
and upsert into the local index."""

import hashlib
import json
import os
import time

from . import sidecar, thumbnail, extract, index, metrics

# When a PDF is dropped into the library, our drop-handler runs
# import_pdf and writes the sidecar + thumbnail. The GFileMonitor
# watching the library directory then fires a CREATED event for the
# same file and would call import_pdf a second time. To avoid
# repeating the slow CrossRef / OpenAlex network calls, import_pdf
# returns early ("recent" status) when it sees that either the
# sidecar or the thumbnail was written within this many seconds.
RECENT_THRESHOLD_SECONDS = 2.0


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def find_pdfs(root):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".pdf"):
                yield os.path.join(dirpath, name)


def _build_record(pdf_path):
    """Run the full extraction pipeline and return a fresh record dict."""
    rec = sidecar.new_record(pdf_path)
    extracted = extract.extract_from_pdf(pdf_path)
    rec["title"] = extracted["title"] or os.path.splitext(
        os.path.basename(pdf_path))[0]
    rec["authors"] = extracted["authors"]
    rec["year"] = extracted["year"]
    rec["doi"] = extracted["doi"]
    rec["journal"] = extracted["journal"]
    rec["raw"] = extracted["raw"]
    return rec


def refresh_pdf(conn, pdf_path):
    """Re-run extraction for an existing PDF, merging the result back into
    the sidecar. Honours hand_edited=True (skipped). User-set fields like
    tags, notes, hand_edited, citations* are preserved.

    Returns (rec, status) where status is 'refreshed', 'hand_edited',
    'no_sidecar', or 'error'.
    """
    sc_path = sidecar.sidecar_path_for(pdf_path)
    if not os.path.isfile(sc_path):
        return None, "no_sidecar"
    try:
        old = sidecar.read(sc_path)
    except Exception as e:
        print("refresh: cannot read sidecar for {}: {}".format(pdf_path, e))
        return None, "error"
    if old.get("hand_edited"):
        return old, "hand_edited"

    fresh = _build_record(pdf_path)
    # Preserve user-curated and history fields.
    for key in ("tags", "notes", "mark", "hand_edited", "added_date",
                "citations", "citations_source", "citations_fetched",
                "citations_by_year",
                "auto_keywords", "abstract", "authorships", "highlights",
                "published_version"):
        if key in old:
            fresh[key] = old[key]
    if not fresh.get("sha256"):
        fresh["sha256"] = old.get("sha256") or _sha256(pdf_path)

    # Re-fetch OpenAlex enrichment so newly-added fields (authorships /
    # abstract / keywords) actually populate during --refresh.
    if fresh.get("doi"):
        n, src, kw, abstract, authorships, cby = metrics.fetch_metrics(fresh["doi"])
        if n is not None:
            fresh["citations"] = n
            fresh["citations_source"] = src
            fresh["citations_fetched"] = metrics.today_iso()
        if kw:
            fresh["auto_keywords"] = kw
        if abstract:
            fresh["abstract"] = abstract
        if authorships:
            fresh["authorships"] = authorships
            oa_names = [a["name"] for a in authorships if a.get("name")]
            if oa_names:
                fresh["authors"] = oa_names
        if cby:
            fresh["citations_by_year"] = cby

    # Preprint → published-version lookup (refresh re-checks too;
    # OpenAlex may have indexed the journal version since last time).
    if metrics.is_preprint_doi(fresh.get("doi")):
        pv = metrics.find_published_version(
            fresh.get("title"), fresh.get("authors") or [], fresh["doi"])
        if pv:
            fresh["published_version"] = pv

    sidecar.write(sc_path, fresh)
    th_path = sidecar.thumb_path_for(pdf_path)
    if not os.path.isfile(th_path):
        thumbnail.make_thumbnail(pdf_path, th_path)
    mtime = os.path.getmtime(sc_path)
    index.upsert(conn, pdf_path, sc_path,
                 th_path if os.path.isfile(th_path) else None,
                 fresh, mtime)
    return fresh, "refreshed"


def import_pdf(conn, pdf_path):
    """Make sure pdf_path has sidecar + thumbnail, and upsert the index row.

    For new PDFs (no sidecar yet), check for duplicates by DOI or SHA-256
    against the existing index and skip them.

    Returns:
        (rec, status) where status is 'new', 'existing', or 'duplicate'.
        For 'duplicate', rec is the *existing* row's dict (so the caller
        can report which file it duplicates).
    """
    sc_path = sidecar.sidecar_path_for(pdf_path)
    th_path = sidecar.thumb_path_for(pdf_path)

    # Recent-import guard: if the sidecar or thumbnail was written in
    # the last RECENT_THRESHOLD_SECONDS seconds, this is almost
    # certainly a duplicate trigger from the GFileMonitor watcher
    # firing right after our own write. Skip to avoid the duplicate
    # CrossRef / OpenAlex call.
    now = time.time()
    for p in (sc_path, th_path):
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if 0 <= age < RECENT_THRESHOLD_SECONDS:
            if os.path.isfile(sc_path):
                return sidecar.read(sc_path), "recent"
            return None, "recent"

    if os.path.isfile(sc_path):
        rec = sidecar.read(sc_path)
        if not rec.get("sha256"):
            rec["sha256"] = _sha256(pdf_path)
            sidecar.write(sc_path, rec)
        thumbnail.make_thumbnail(pdf_path, th_path)
        mtime = os.path.getmtime(sc_path)
        index.upsert(conn, pdf_path, sc_path,
                     th_path if os.path.isfile(th_path) else None,
                     rec, mtime)
        return rec, "existing"

    # New PDF: hash first so we can detect renames cheaply.
    sha = _sha256(pdf_path)
    by_hash = index.find_duplicate(conn, sha256=sha, exclude_path=pdf_path)
    if by_hash and not os.path.isfile(by_hash["pdf_path"]):
        # The byte-identical entry's PDF is gone from disk → this is a
        # rename. Adopt the existing row instead of creating a new one.
        return _adopt_renamed(conn, pdf_path, by_hash, sha), "renamed"

    # Otherwise extract metadata and proceed with normal dedup logic.
    rec = _build_record(pdf_path)
    rec["sha256"] = sha
    dup = by_hash or index.find_duplicate(conn, doi=rec.get("doi"),
                                          exclude_path=pdf_path)
    if dup:
        return dup, "duplicate"

    # OpenAlex enrichment (one HTTP, four outputs). Best-effort; failures
    # leave fields untouched.
    if rec.get("doi"):
        n, src, kw, abstract, authorships, cby = metrics.fetch_metrics(rec["doi"])
        if n is not None:
            rec["citations"] = n
            rec["citations_source"] = src
            rec["citations_fetched"] = metrics.today_iso()
        if kw:
            rec["auto_keywords"] = kw
        if abstract:
            rec["abstract"] = abstract
        if authorships:
            rec["authorships"] = authorships
            # Prefer OpenAlex display names for the flat authors list.
            oa_names = [a["name"] for a in authorships if a.get("name")]
            if oa_names:
                rec["authors"] = oa_names
        if cby:
            rec["citations_by_year"] = cby

    # Preprint → published-version lookup. One extra OpenAlex call,
    # only for preprint DOIs.
    if metrics.is_preprint_doi(rec.get("doi")):
        pv = metrics.find_published_version(
            rec.get("title"), rec.get("authors") or [], rec["doi"])
        if pv:
            rec["published_version"] = pv

    sidecar.write(sc_path, rec)
    thumbnail.make_thumbnail(pdf_path, th_path)
    mtime = os.path.getmtime(sc_path)
    index.upsert(conn, pdf_path, sc_path,
                 th_path if os.path.isfile(th_path) else None,
                 rec, mtime)
    return rec, "new"


def _adopt_renamed(conn, new_path, old_row, sha):
    """An existing entry's PDF has moved to new_path. Move its sidecar
    and thumbnail (if still around), update the row, and return the
    record."""
    new_sc = sidecar.sidecar_path_for(new_path)
    new_th = sidecar.thumb_path_for(new_path)

    old_sc = old_row.get("sidecar_path")
    if old_sc and os.path.isfile(old_sc):
        rec = sidecar.read(old_sc)
        try:
            os.remove(old_sc)
        except OSError:
            pass
    else:
        # Reconstruct the record from the DB row.
        rec = sidecar.new_record(new_path)
        rec["title"] = old_row.get("title")
        try:
            rec["authors"] = json.loads(old_row.get("authors_json") or "[]")
        except (TypeError, ValueError):
            rec["authors"] = []
        rec["year"] = old_row.get("year")
        rec["doi"] = old_row.get("doi")
        rec["journal"] = old_row.get("journal")
        try:
            rec["tags"] = json.loads(old_row.get("tags_json") or "[]")
        except (TypeError, ValueError):
            rec["tags"] = []
        rec["citations"] = old_row.get("citations")
        rec["citations_source"] = old_row.get("citations_source")
        rec["citations_fetched"] = old_row.get("citations_fetched")

    rec["pdf_filename"] = os.path.basename(new_path)
    rec["sha256"] = sha
    sidecar.write(new_sc, rec)

    old_th = old_row.get("thumb_path")
    if old_th and os.path.isfile(old_th) and not os.path.isfile(new_th):
        try:
            os.rename(old_th, new_th)
        except OSError:
            thumbnail.make_thumbnail(new_path, new_th)
    elif not os.path.isfile(new_th):
        thumbnail.make_thumbnail(new_path, new_th)

    mtime = os.path.getmtime(new_sc)
    index.upsert(conn, new_path, new_sc,
                 new_th if os.path.isfile(new_th) else None,
                 rec, mtime)
    if old_row.get("pdf_path") and old_row["pdf_path"] != new_path:
        index.remove(conn, old_row["pdf_path"])
    return rec


def delete_pdf(conn, pdf_path):
    """Remove the PDF, its sidecar, its thumbnail, and the index row."""
    sc = sidecar.sidecar_path_for(pdf_path)
    th = sidecar.thumb_path_for(pdf_path)
    for p in (pdf_path, sc, th):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError as e:
            print("delete error for {}: {}".format(p, e))
    index.remove(conn, pdf_path)


def rename_pdf(conn, old_path, new_path):
    """Rename a PDF in place (keeps the same directory unless new_path
    specifies otherwise). Moves sidecar and thumbnail too, and updates
    the index. Raises FileExistsError if the destination is taken."""
    old_path = os.path.abspath(old_path)
    new_path = os.path.abspath(new_path)
    if old_path == new_path:
        return
    if os.path.exists(new_path):
        raise FileExistsError(new_path)

    old_sc = sidecar.sidecar_path_for(old_path)
    new_sc = sidecar.sidecar_path_for(new_path)
    old_th = sidecar.thumb_path_for(old_path)
    new_th = sidecar.thumb_path_for(new_path)

    os.rename(old_path, new_path)

    rec = None
    if os.path.isfile(old_sc):
        rec = sidecar.read(old_sc)
        rec["pdf_filename"] = os.path.basename(new_path)
        sidecar.write(new_sc, rec)
        try:
            os.remove(old_sc)
        except OSError:
            pass

    if os.path.isfile(old_th):
        try:
            os.rename(old_th, new_th)
        except OSError:
            pass

    if rec is not None:
        mtime = os.path.getmtime(new_sc)
        index.upsert(conn, new_path, new_sc,
                     new_th if os.path.isfile(new_th) else None,
                     rec, mtime)
    index.remove(conn, old_path)


def import_tree(conn, root, on_progress=None, refresh=False):
    """Import every PDF under root. With refresh=True, sidecars without
    hand_edited=True are re-extracted (preserving tags / notes / citations).
    on_progress(i, n, path, rec, status) optional callback."""
    pdfs = list(find_pdfs(root))
    n = len(pdfs)
    for i, p in enumerate(pdfs):
        rec, status = None, "error"
        try:
            if refresh:
                rec, status = refresh_pdf(conn, p)
                if status == "no_sidecar":
                    rec, status = import_pdf(conn, p)
            else:
                rec, status = import_pdf(conn, p)
        except Exception as e:
            print("import failed for {}: {}".format(p, e))
        if on_progress:
            on_progress(i + 1, n, p, rec, status)
    return n
