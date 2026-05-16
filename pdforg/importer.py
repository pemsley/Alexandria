"""Walk a directory tree, ensure each PDF has a sidecar + thumbnail,
and upsert into the local index."""

import hashlib
import json
import os
import re
import shutil
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


def _is_inside(path, root):
    """True if `path` is at or below `root` (after symlink resolution)."""
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
    except OSError:
        return False
    rr_with_sep = rr.rstrip(os.sep) + os.sep
    return rp == rr or rp.startswith(rr_with_sep)


def stage_into_library(src_path, library_root, conn=None):
    """Ensure `src_path` lives inside `library_root`, copying it in if
    necessary. Used by the user-driven Import flow so picked PDFs get
    consolidated under the library root rather than scattering sidecar
    JSON next to wherever the user happened to keep them. Required for
    the Flatpak build: with `--filesystem=xdg-documents`, files chosen
    via the FileChooser portal are bind-mounted at a transient
    `/run/user/<uid>/doc/...` path, and we need a real copy under the
    library before the portal mount goes away.

    Returns `(path, status)` where status is one of:
        'inplace'   — already inside library_root, no copy needed
        'copied'    — copy succeeded; path is the new in-library path
        'duplicate' — same SHA already indexed; path is the existing
                      in-library file, no copy made

    On a name collision with different content the copy is renamed
    `<stem>-<sha8>.pdf` so it doesn't clobber an unrelated file.
    The dedup gate runs *before* the copy so we never strand a
    redundant file in the library."""
    library_root = os.path.expanduser(library_root)
    os.makedirs(library_root, exist_ok=True)
    if _is_inside(src_path, library_root):
        return src_path, "inplace"
    sha = _sha256(src_path)
    if conn is not None:
        dup = index.find_duplicate(conn, sha256=sha)
        if dup and dup.get("pdf_path") and os.path.isfile(dup["pdf_path"]):
            return dup["pdf_path"], "duplicate"
    basename = os.path.basename(src_path)
    target = os.path.join(library_root, basename)
    if os.path.exists(target):
        # Different content, same name (we'd have hit the SHA dup gate
        # above otherwise). Suffix with the first 8 hex of the hash so
        # both files coexist.
        stem, ext = os.path.splitext(basename)
        target = os.path.join(
            library_root, "{}-{}{}".format(stem, sha[:8], ext))
    shutil.copy2(src_path, target)
    return target, "copied"


def _title_token_set(title):
    """Lowercased significant-word set, used by
    `_openalex_record_matches` to compare PDF and OpenAlex titles
    with a token-overlap rule. Stop-words common to many titles
    are dropped so they don't carry the match."""
    if not title:
        return set()
    words = re.findall(r"[\w']+", title.lower())
    stops = {"a", "an", "the", "of", "and", "or", "in", "on", "at",
             "for", "to", "by", "with", "from", "as", "via",
             "study", "analysis", "using", "based"}
    return {w for w in words if len(w) > 2 and w not in stops}


def _openalex_record_matches(pdf_title, pdf_year, oa_title, oa_year):
    """True if the OpenAlex Work's metadata is consistent with the
    PDF's. Used by `import_pdf` to detect cross-contaminated OpenAlex
    records (DOI right, but title/authors/year from a different
    paper).

    Pass-conditions are intentionally lenient — we only want to
    *reject* clear mismatches:

    - When OpenAlex didn't return a title or year, we can't compare,
      so we trust it (preserves the historic behaviour).
    - Year mismatch > 1 fails — print/online publication years
      differ by at most one in normal practice.
    - Title token-overlap: shared significant tokens / smaller-set
      size must be ≥ 0.3, OR there must be at least two shared
      tokens. The two-token floor saves 2-word titles from being
      mis-rejected on a single shared word; the ratio threshold
      catches the cross-contamination case (zero or one shared
      token between an ant-biology title and a crystallography
      title).
    """
    # Year check: if both sides have a year and they're more than 1
    # apart, the records refer to different papers.
    if pdf_year and oa_year:
        try:
            if abs(int(pdf_year) - int(oa_year)) > 1:
                return False
        except (TypeError, ValueError):
            pass
    # Title check: only meaningful when both sides have titles.
    pdf_set = _title_token_set(pdf_title)
    oa_set = _title_token_set(oa_title)
    if not pdf_set or not oa_set:
        return True
    shared = pdf_set & oa_set
    if len(shared) >= 2:
        return True
    smaller = min(len(pdf_set), len(oa_set))
    if smaller > 0 and len(shared) / smaller >= 0.3:
        return True
    return False


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
                "published_version",
                "bibtex_key", "bibtex_type", "bibtex_extra"):
        if key in old:
            fresh[key] = old[key]
    if not fresh.get("sha256"):
        fresh["sha256"] = old.get("sha256") or _sha256(pdf_path)

    # Re-fetch OpenAlex enrichment so newly-added fields (authorships /
    # abstract / keywords) actually populate during --refresh.
    if fresh.get("doi"):
        (n, src, kw, abstract, authorships, cby,
         oa_title, oa_year, is_oa, oa_status) = metrics.fetch_metrics(
             fresh["doi"])
        if _openalex_record_matches(
                fresh.get("title"), fresh.get("year"),
                oa_title, oa_year):
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
            if is_oa is not None:
                fresh["is_oa"] = is_oa
            if oa_status:
                fresh["oa_status"] = oa_status
        elif oa_title or oa_year:
            print("[importer] OpenAlex record for {} looks corrupted "
                  "(refresh) — keeping existing metadata".format(
                      fresh["doi"]))

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
        thumbnail.make_thumbnail(pdf_path, th_path,
                                 title=fresh.get("title"))
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
        thumbnail.make_thumbnail(pdf_path, th_path,
                                 title=rec.get("title"))
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

    if by_hash:
        # Byte-identical copy of a PDF already in the library and the
        # original is still on disk. Skip _build_record's poppler
        # extraction — we already know the metadata.
        return by_hash, "duplicate"

    # No hash match: extract metadata and dedup by DOI.
    rec = _build_record(pdf_path)
    rec["sha256"] = sha
    dup = index.find_duplicate(conn, doi=rec.get("doi"),
                               exclude_path=pdf_path)
    if dup:
        return dup, "duplicate"

    # OpenAlex enrichment (one HTTP, six outputs). Best-effort;
    # failures leave fields untouched.
    if rec.get("doi"):
        (n, src, kw, abstract, authorships, cby,
         oa_title, oa_year, is_oa, oa_status) = metrics.fetch_metrics(
             rec["doi"])
        # OpenAlex sanity-check: rare but real, an OpenAlex Work
        # record cross-contaminates two papers — the DOI is right
        # but the title/authors/year come from a different paper.
        # When that happens we DON'T want to clobber the
        # PDF-extracted authors / abstract / keywords with the wrong
        # ones (and the citation count for a conflated record is
        # untrustworthy too). Detect by comparing PDF title vs
        # OpenAlex title; if they share no significant tokens, or
        # the years differ by more than 1, treat the OpenAlex
        # record as suspect and skip the override.
        if _openalex_record_matches(
                rec.get("title"), rec.get("year"), oa_title, oa_year):
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
                # Prefer OpenAlex display names for the flat
                # authors list.
                oa_names = [a["name"] for a in authorships if a.get("name")]
                if oa_names:
                    rec["authors"] = oa_names
            if cby:
                rec["citations_by_year"] = cby
            if is_oa is not None:
                rec["is_oa"] = is_oa
            if oa_status:
                rec["oa_status"] = oa_status
        elif oa_title or oa_year:
            print("[importer] OpenAlex record for {} looks corrupted "
                  "(PDF: {!r}/{} vs OpenAlex: {!r}/{}) — keeping "
                  "PDF-extracted metadata".format(
                      rec["doi"],
                      rec.get("title"), rec.get("year"),
                      oa_title, oa_year))

    # Preprint → published-version lookup. One extra OpenAlex call,
    # only for preprint DOIs.
    if metrics.is_preprint_doi(rec.get("doi")):
        pv = metrics.find_published_version(
            rec.get("title"), rec.get("authors") or [], rec["doi"])
        if pv:
            rec["published_version"] = pv

    sidecar.write(sc_path, rec)
    thumbnail.make_thumbnail(pdf_path, th_path, title=rec.get("title"))
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
            thumbnail.make_thumbnail(new_path, new_th,
                                     title=rec.get("title"))
    elif not os.path.isfile(new_th):
        thumbnail.make_thumbnail(new_path, new_th,
                                 title=rec.get("title"))

    mtime = os.path.getmtime(new_sc)
    index.upsert(conn, new_path, new_sc,
                 new_th if os.path.isfile(new_th) else None,
                 rec, mtime)
    if old_row.get("pdf_path") and old_row["pdf_path"] != new_path:
        index.remove(conn, old_row["pdf_path"])
    return rec


def delete_pdf(conn, pdf_path):
    """Remove the PDF, its sidecar, its thumbnail, and the index row.

    For a *ghost* (BibTeX-only) entry whose `pdf_path` is a synthetic
    `bibtex:<key>` identifier, only the sidecar (in
    `LIBRARY_ROOT/.alexandria-bibtex/`) and the index row are
    removed — there is no PDF or thumbnail on disk. We look up the
    sidecar path from the index row rather than deriving it, since
    ghost sidecars don't live next to a real PDF."""
    sc = None
    try:
        cur = conn.execute(
            "SELECT sidecar_path FROM papers WHERE pdf_path = ?",
            (pdf_path,))
        row = cur.fetchone()
        if row is not None:
            sc = row["sidecar_path"]
    except Exception:
        pass
    if not sc:
        sc = sidecar.sidecar_path_for(pdf_path)
    th = sidecar.thumb_path_for(pdf_path)
    paths = [sc, th]
    if not sidecar.is_ghost_path(pdf_path):
        paths.insert(0, pdf_path)
    for p in paths:
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
