"""FastMCP app and tool registrations.

v0: ping (diagnostic).
v1: search_library, find_by_dois, get_papers, get_sidecars —
    enough for the LLM to find, identify, and inspect papers in
    the user's library.

All read tools take plural arguments by default (the LLM that
wants one paper passes `[1234]`); the single write tool will
land in the next batch, after the BEGIN-IMMEDIATE rendezvous
protocol is in place."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Optional

from mcp.server.fastmcp import FastMCP

import re
import tempfile
import urllib.parse

from alexandria import (bibtex_import, index, metrics, pdf_fetch,
                        pdf_text, sidecar)

from . import __version__, config, db
from .models import PaperDetail, PaperSummary, PdfText


mcp = FastMCP("Alexandria")


# ---- helpers --------------------------------------------------------

def _normalize_doi(s: str) -> str:
    """Strip the URL prefix CrossRef and OpenAlex add to DOIs, plus
    surrounding whitespace; lowercase. Mirrors the normalisation
    `alexandria.metrics` already applies."""
    if not s:
        return ""
    v = s.strip()
    low = v.lower()
    for p in ("https://doi.org/", "http://doi.org/"):
        if low.startswith(p):
            v = v[len(p):]
            break
    return v.strip().lower()


def _row_to_summary(row) -> PaperSummary:
    return PaperSummary(
        id=row["id"],
        title=row["title"],
        first_author=row["first_author"],
        last_author=row["last_author"],
        year=row["year"],
        journal=row["journal"],
        doi=row["doi"],
        citations=row["citations"],
        is_ghost=sidecar.is_ghost_path(row["pdf_path"]),
    )


_FTS5_OPERATOR_CHARS = '"-+^*():'


def _fts5_sanitize(query: str) -> str:
    """Turn a free-text user query into something FTS5 can parse.

    FTS5 treats `"`, `-`, `+`, `^`, `*`, `(`, `)`, `:` as operators.
    A query like "cryo-EM" parses as `cryo MINUS EM` and errors out
    on the missing `EM` column. Replace each operator with a space
    so terms become an implicit AND list — matches what the GUI
    search bar already does, and is what an LLM caller naturally
    expects from a `query` field."""
    if not query:
        return ""
    for ch in _FTS5_OPERATOR_CHARS:
        query = query.replace(ch, " ")
    return " ".join(query.split())


def _safe_json_list(s: Optional[str]) -> list:
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def _row_to_detail(row) -> PaperDetail:
    is_oa_raw = row["is_oa"]
    return PaperDetail(
        id=row["id"],
        title=row["title"],
        first_author=row["first_author"],
        last_author=row["last_author"],
        year=row["year"],
        journal=row["journal"],
        doi=row["doi"],
        citations=row["citations"],
        is_ghost=sidecar.is_ghost_path(row["pdf_path"]),
        authors=_safe_json_list(row["authors_json"]),
        abstract=row["abstract"],
        tags=_safe_json_list(row["tags_json"]),
        mark=row["mark"],
        is_oa=None if is_oa_raw is None else bool(is_oa_raw),
        oa_status=row["oa_status"],
        license_label=row["license_label"],
        added_date=row["added_date"],
        pdf_path=row["pdf_path"],
        sidecar_path=row["sidecar_path"],
        citations_by_year=_safe_json_list(row["citations_by_year_json"]),
        funders=_safe_json_list(row["funders_json"]),
        grants=_safe_json_list(row["grants_json"]),
    )


# ---- tools ----------------------------------------------------------

@mcp.tool()
def ping() -> dict:
    """Diagnostic: confirm the MCP server is up and report which
    Alexandria library it's pointed at.

    Returns library_root, db_path, db_exists, paper_count,
    readonly, mcp_server_version. Use this first in any session
    to verify the server sees the library you expect."""
    out = {
        "library_root": config.library_root(),
        "db_path": config.db_path(),
        "db_exists": os.path.isfile(config.db_path()),
        "paper_count": None,
        "readonly": config.readonly(),
        "mcp_server_version": __version__,
    }
    if out["db_exists"]:
        try:
            conn = db.get_ro_connection()
            row = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()
            out["paper_count"] = int(row["n"])
        except Exception as e:
            out["paper_count_error"] = str(e)
    return out


@mcp.tool()
def search_library(
    query: str,
    limit: int = 20,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> list[dict]:
    """Full-text search across the user's PDF library. Searches
    title, authors, abstract, journal, DOI, auto-keywords, and any
    text the user has highlighted or commented on inside the PDFs.

    Returns a list of paper summaries ordered by FTS relevance,
    capped at `limit` (default 20). Use `get_papers` to fetch
    full records for any IDs returned here.

    The user has read or curated every paper this returns — this
    is *their* library, not the wider literature."""
    safe = _fts5_sanitize(query)
    if not safe:
        return []
    conn = db.get_ro_connection()
    # FTS5 MATCH against the papers_fts virtual table, then join
    # back to papers for the summary columns. The double join via
    # rowid is how FTS5 carries the paper id through.
    sql = """
        SELECT p.* , papers_fts.rank AS _rank
        FROM papers_fts
        JOIN papers AS p ON p.id = papers_fts.rowid
        WHERE papers_fts MATCH ?
    """
    params: list = [safe]
    if year_from is not None:
        sql += " AND p.year >= ?"
        params.append(int(year_from))
    if year_to is not None:
        sql += " AND p.year <= ?"
        params.append(int(year_to))
    sql += " ORDER BY papers_fts.rank LIMIT ?"
    params.append(max(1, min(int(limit), 100)))
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        # Invalid FTS5 query syntax is the most common failure
        # (unbalanced quotes, lone operators). Surface the error
        # so the LLM can rephrase rather than silently empty-result.
        raise ValueError("search failed: {}".format(e)) from e
    return [asdict(_row_to_summary(r)) for r in rows]


@mcp.tool()
def find_by_dois(dois: list[str]) -> list[Optional[dict]]:
    """Look up papers by DOI. Returns a list the same length as
    `dois`, with `None` in positions where no paper matches — so
    the caller can correlate inputs to outputs by index.

    Each DOI is normalised (lowercased, `https://doi.org/`
    stripped) before lookup. Useful for "here are five DOIs from a
    references list — which do I have?" workflows."""
    if not dois:
        return []
    conn = db.get_ro_connection()
    out: list[Optional[dict]] = []
    for raw in dois:
        norm = _normalize_doi(raw)
        if not norm:
            out.append(None)
            continue
        row = conn.execute(
            "SELECT * FROM papers WHERE lower(doi) = ? LIMIT 1",
            (norm,),
        ).fetchone()
        out.append(asdict(_row_to_summary(row)) if row else None)
    return out


@mcp.tool()
def get_papers(paper_ids: list[int]) -> list[dict]:
    """Fetch full paper records for the given IDs. Returns one
    PaperDetail per input ID in the same order. Raises if any ID
    is unknown — check membership via `search_library` or
    `find_by_dois` first if uncertain.

    Capped at 50 IDs per call to keep payloads bounded.

    For fields the database doesn't promote to columns — raw
    OpenAlex payloads, comments, hand-edited custom fields —
    follow up with `get_sidecars`, which returns the canonical
    record."""
    if not paper_ids:
        return []
    if len(paper_ids) > 50:
        raise ValueError(
            "get_papers: at most 50 IDs per call ({} given)".format(
                len(paper_ids)))
    conn = db.get_ro_connection()
    placeholders = ",".join("?" for _ in paper_ids)
    rows = conn.execute(
        "SELECT * FROM papers WHERE id IN ({})".format(placeholders),
        list(paper_ids),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    missing = [pid for pid in paper_ids if pid not in by_id]
    if missing:
        raise ValueError(
            "get_papers: unknown ID(s) {}".format(missing))
    return [asdict(_row_to_detail(by_id[pid])) for pid in paper_ids]


def _openalex_work(doi: str) -> Optional[dict]:
    """Fetch the raw OpenAlex Work record for `doi`. Returns the
    dict on success, None on 404 / network failure. The MCP write
    tool needs more fields than `metrics.fetch_metrics` exposes
    (journal, OA URL, authorships in OpenAlex shape), so it pulls
    the record directly."""
    qdoi = urllib.parse.quote(doi, safe="")
    url = "https://api.openalex.org/works/doi:" + qdoi
    if metrics.OPENALEX_MAILTO:
        url += "?mailto=" + urllib.parse.quote(metrics.OPENALEX_MAILTO)
    return metrics._http_get_json(
        url,
        headers={"User-Agent": metrics.OPENALEX_UA,
                 "Accept": "application/json"},
        timeout=15)


_BIBTEX_KEY_STRIP = re.compile(r"[^A-Za-z0-9]")


def _bibtex_key_from(first_author: str, year, title: str) -> str:
    """surnameYEARfirstword — mirrors the heuristic in
    `discover._on_add_work` so MCP-imported papers carry keys that
    look like the GUI-imported ones."""
    surname = ""
    if first_author:
        parts = first_author.split()
        if parts:
            surname = _BIBTEX_KEY_STRIP.sub("", parts[-1]).lower()
    yr = str(year or "")
    first_word = ""
    if title:
        toks = title.split()
        if toks:
            first_word = _BIBTEX_KEY_STRIP.sub("", toks[0]).lower()
    return (surname + yr + first_word) or "openalex"


@mcp.tool()
def add_paper_by_doi(doi: str, fetch_pdf: bool = True) -> dict:
    """Add a paper to the library by DOI.

    Looks up the DOI on OpenAlex for metadata, creates a ghost
    (BibTeX-only) entry first, then — when `fetch_pdf` is true —
    tries to fetch an open-access PDF (OpenAlex → Unpaywall →
    EuropePMC, in that order). EuropePMC is the load-bearing
    fallback for Cloudflare-protected publishers (Nature, Cell,
    Science): for NIH/UKRI-funded papers it serves the
    PMC-deposited copy that publisher downloads won't.

    On a successful PDF fetch the ghost is replaced by a normal
    entry. On failure the ghost stays — the user can attempt
    `Get PDF` later from the GUI or supply a PDF manually.

    Refuses if the server is in read-only mode
    (ALEXANDRIA_READONLY).

    Returns:
        doi, normalised_doi, status (one of: 'imported_with_pdf',
        'ghost', 'already_in_library', 'no_openalex_record',
        'error'), paper_id (when known), pdf_source (URL that
        worked, when applicable), message (human-readable).
    """
    if config.readonly():
        raise ValueError("server is in read-only mode")
    if not doi or not doi.strip():
        raise ValueError("doi must be a non-empty string")

    norm = _normalize_doi(doi)
    out: dict = {"doi": doi, "normalised_doi": norm}

    # Already in the library? Cheap check before any network work.
    conn = db.get_ro_connection()
    row = conn.execute(
        "SELECT id, pdf_path FROM papers WHERE lower(doi) = ? LIMIT 1",
        (norm,),
    ).fetchone()
    if row:
        out["status"] = "already_in_library"
        out["paper_id"] = row["id"]
        out["message"] = "DOI {} already imported as paper {}".format(
            norm, row["id"])
        return out

    # Pull OpenAlex metadata for the BibTeX-shape dict.
    data = _openalex_work(norm)
    if not data:
        out["status"] = "no_openalex_record"
        out["message"] = ("OpenAlex has no record for this DOI "
                          "(fresh DOI? typo?) — nothing imported")
        return out
    title = (data.get("title") or data.get("display_name") or "").strip()
    year = data.get("publication_year")
    primary = data.get("primary_location") or {}
    source = primary.get("source") or {}
    journal = (source.get("display_name") or "").strip() or None
    authors = []
    for a in (data.get("authorships") or []):
        nm = ((a.get("author") or {}).get("display_name") or "").strip()
        if nm:
            authors.append(nm)
    first_author = authors[0] if authors else ""
    bibtex_key = _bibtex_key_from(first_author, year, title)

    br = {
        "title": title or None,
        "authors": authors,
        "year": year,
        "journal": journal,
        "doi": norm,
        "bibtex_key": bibtex_key,
        "bibtex_type": "article",
        "bibtex_extra": {},
        "file": None,
    }

    # Open a writable connection for the import (the read-only
    # connection from db.py refuses inserts).
    rw_conn = bibtex_import.index.open_db()
    try:
        rec, status = bibtex_import.import_record(
            rw_conn, br, config.library_root())
    except Exception as e:
        out["status"] = "error"
        out["message"] = "import_record failed: {}".format(e)
        return out

    if status == "duplicate":
        # `import_record` saw it under a different DOI normalisation;
        # surface as already_in_library.
        out["status"] = "already_in_library"
        dup = rw_conn.execute(
            "SELECT id FROM papers WHERE lower(doi) = ? LIMIT 1",
            (norm,)).fetchone()
        if dup:
            out["paper_id"] = dup["id"]
        out["message"] = "already in library"
        return out
    if status == "error" or rec is None:
        out["status"] = "error"
        out["message"] = "could not import (status={})".format(status)
        return out

    ghost_row = rw_conn.execute(
        "SELECT * FROM papers WHERE lower(doi) = ? LIMIT 1",
        (norm,)).fetchone()
    if ghost_row is None:
        out["status"] = "error"
        out["message"] = "ghost row not found after import"
        return out
    out["paper_id"] = ghost_row["id"]

    if not fetch_pdf:
        out["status"] = "ghost"
        out["message"] = "imported as ghost (fetch_pdf=False)"
        return out

    # Try the OA download chain. tmp file → attach_pdf_to_ghost.
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        ok, url_used, dl_msg = pdf_fetch.fetch_oa_pdf(norm, tmp_path)
        if not ok:
            out["status"] = "ghost"
            out["message"] = ("imported as ghost; PDF fetch failed "
                              "({})".format(dl_msg))
            return out
        try:
            _new_path, attach_status, attach_msg = (
                bibtex_import.attach_pdf_to_ghost(
                    rw_conn, dict(ghost_row), tmp_path,
                    config.library_root()))
        except Exception as e:
            out["status"] = "ghost"
            out["message"] = ("PDF downloaded but attach failed: "
                              "{}".format(e))
            return out
        if attach_status == "merged":
            merged = rw_conn.execute(
                "SELECT id FROM papers WHERE lower(doi) = ? LIMIT 1",
                (norm,)).fetchone()
            if merged:
                out["paper_id"] = merged["id"]
            out["status"] = "imported_with_pdf"
            out["pdf_source"] = url_used
            out["message"] = "imported with PDF from {}".format(url_used)
        else:
            out["status"] = "ghost"
            out["message"] = ("PDF downloaded but ghost merge "
                              "rejected: {}".format(attach_msg))
        return out
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@mcp.tool()
def get_sidecars(paper_ids: list[int]) -> list[dict]:
    """Return the raw `.alexandria` sidecar JSON for each paper,
    one per input ID, in input order.

    The sidecar is the *source of truth* for paper metadata; the
    indexed database columns are derived from it. Anything the user
    has stored about a paper — including fields the database
    doesn't surface (raw enrichment payloads, comments, custom
    fields, anything hand-edited) — lives here.

    Works for ghost (BibTeX-only) entries too — their sidecars live
    in `.alexandria-bibtex/`.

    Capped at 50 IDs per call to keep payloads bounded."""
    if not paper_ids:
        return []
    if len(paper_ids) > 50:
        raise ValueError(
            "get_sidecars: at most 50 IDs per call ({} given)".format(
                len(paper_ids)))
    conn = db.get_ro_connection()
    placeholders = ",".join("?" for _ in paper_ids)
    rows = conn.execute(
        "SELECT id, sidecar_path FROM papers WHERE id IN ({})".format(
            placeholders),
        list(paper_ids),
    ).fetchall()
    by_id = {r["id"]: r["sidecar_path"] for r in rows}
    missing = [pid for pid in paper_ids if pid not in by_id]
    if missing:
        raise ValueError(
            "get_sidecars: unknown ID(s) {}".format(missing))
    out: list[dict] = []
    for pid in paper_ids:
        try:
            out.append(sidecar.read(by_id[pid]))
        except Exception as e:
            out.append({"_error": "sidecar read failed: {}".format(e),
                        "_sidecar_path": by_id[pid]})
    return out


@mcp.tool()
def recently_added(days: int = 7, limit: int = 50) -> list[dict]:
    """Papers added to the library in the last `days` days,
    most-recently-added first.

    `added_date` in the index is stored at day resolution
    (YYYY-MM-DD), so smaller windows than a day aren't
    expressible: `days=1` returns everything added today,
    `days=7` covers a rolling week, `days=30` a month, etc.

    Useful for "what's new in my library since last week",
    "what did I just import", or catching up after a batch
    import. Pair with `get_papers` for full records on any IDs
    that look interesting."""
    days = max(1, min(int(days), 3650))
    limit = max(1, min(int(limit), 200))
    conn = db.get_ro_connection()
    rows = conn.execute(
        "SELECT * FROM papers "
        "WHERE added_date IS NOT NULL "
        "  AND added_date >= date('now', ?) "
        "ORDER BY added_date DESC, id DESC "
        "LIMIT ?",
        ("-{} days".format(days - 1), limit),
    ).fetchall()
    return [asdict(_row_to_summary(r)) for r in rows]


@mcp.tool()
def get_citation_neighbourhood(
    paper_id: int,
    citers_limit: int = 25,
    citers_sort: str = "cited",
    refs_limit: int = 50,
) -> dict:
    """One-hop citation graph around a library paper. Returns the
    seed paper plus its OpenAlex-known references (papers the seed
    cites) and citers (papers that cite the seed), each annotated
    with whether they're already in the user's library.

    Use this when the conversation turns to "what does this paper
    build on?" / "who's picked this up?" / "what in my library
    connects to this?". The library-local cross-references are
    the interesting bit — `in_library: true` rows are papers the
    user has already engaged with that sit in the same
    conversation.

    No edges between refs / citers in this version — those need
    one OpenAlex call per node and would dominate the cost.

    Arguments:
        paper_id     — library row id of the seed.
        citers_limit — top-N most-cited (or most-recent) papers
                       that cite the seed. OpenAlex can serve
                       thousands for popular papers; the cap is
                       what keeps the response readable.
        citers_sort  — 'cited' (default; most-cited first, useful
                       for influence) or 'recent' (newest first,
                       useful for "who's working on this now").
        refs_limit   — at most N references back from the seed.
                       Most papers have 20–80; default 50 covers
                       the typical case without flooding.

    Returns:
        seed: {paper_id, title, year, doi, journal} — your row.
        references: list of {openalex_id, doi, title, year,
            journal, citations, first_author, last_author,
            in_library, library_paper_id}.
        citers: same shape as `references`.
        stats: counts (n_references, n_citers, in_library_*).
    """
    if citers_sort not in ("cited", "recent"):
        raise ValueError(
            "citers_sort must be 'cited' or 'recent' ({} given)".format(
                citers_sort))
    citers_limit = max(1, min(int(citers_limit), 200))
    refs_limit = max(1, min(int(refs_limit), 200))

    conn = db.get_ro_connection()
    seed_row = conn.execute(
        "SELECT id, title, year, doi, journal FROM papers WHERE id = ?",
        (paper_id,)).fetchone()
    if seed_row is None:
        raise ValueError("unknown paper_id {}".format(paper_id))
    doi = seed_row["doi"]
    if not doi:
        raise ValueError(
            "paper {} has no DOI — citation graph needs one to "
            "look up OpenAlex".format(paper_id))

    refs, _refs_source = metrics.fetch_references(
        doi=doi, limit=refs_limit)
    citers = metrics.fetch_cited_by(
        doi=doi, sort=citers_sort, limit=citers_limit)

    # In-library annotation. One query against papers.doi, then a
    # dict lookup per ref/citer.
    lib_dois: dict = {}
    for r in conn.execute(
            "SELECT id, doi FROM papers "
            "WHERE doi IS NOT NULL AND doi <> ''").fetchall():
        lib_dois[r["doi"].lower()] = r["id"]

    def _annotate(rows):
        n_in = 0
        for w in rows:
            d = (w.get("doi") or "").lower()
            lp = lib_dois.get(d)
            w["in_library"] = lp is not None
            w["library_paper_id"] = lp
            if lp is not None:
                n_in += 1
        return n_in

    n_refs_in = _annotate(refs)
    n_citers_in = _annotate(citers)

    return {
        "seed": {
            "paper_id": seed_row["id"],
            "title": seed_row["title"],
            "year": seed_row["year"],
            "doi": seed_row["doi"],
            "journal": seed_row["journal"],
        },
        "references": refs,
        "citers": citers,
        "stats": {
            "n_references": len(refs),
            "n_citers": len(citers),
            "in_library_references": n_refs_in,
            "in_library_citers": n_citers_in,
        },
    }


@mcp.tool()
def get_pdf_texts(
    paper_ids: list[int],
    page_from: int = 1,
    page_to: Optional[int] = None,
    max_chars: int = 50000,
) -> list[dict]:
    """Extract plain text from each paper's PDF, sliced to a page
    range and truncated at `max_chars`. Returns one PdfText per
    input ID in the same order.

    The page bounds apply to *every* paper in the call — almost
    always what you want (specific pages of one paper, or the
    first pages of many). For per-paper page ranges, make
    multiple calls.

    Use this when paper-level metadata (title / abstract /
    keywords from `get_papers`) isn't enough — when you need to
    quote a specific result, compare methods across two papers,
    or check what a paper *actually* says vs the abstract's
    summary. Don't reach for it for every question: 50 KB of
    text per paper times several papers fills the context fast.
    Start with abstracts, drill into the PDFs only when needed.

    Ghost (BibTeX-only) entries return `error="ghost entry — no
    PDF available"`; missing files return `error="pdf not found"`.
    Don't raise an MCP error for these — the caller gets a list
    aligned with the input.

    Capped at 20 IDs per call (each can return up to `max_chars`,
    so a batch of 20 at the default 50 KB is already 1 MB).
    """
    if not paper_ids:
        return []
    if len(paper_ids) > 20:
        raise ValueError(
            "get_pdf_texts: at most 20 IDs per call ({} given)".format(
                len(paper_ids)))
    if page_from < 1:
        page_from = 1
    if page_to is not None and page_to < page_from:
        raise ValueError(
            "get_pdf_texts: page_to ({}) < page_from ({})".format(
                page_to, page_from))
    if max_chars < 100:
        raise ValueError(
            "get_pdf_texts: max_chars must be >= 100 ({} given)".format(
                max_chars))

    conn = db.get_ro_connection()
    placeholders = ",".join("?" for _ in paper_ids)
    rows = conn.execute(
        "SELECT id, pdf_path FROM papers WHERE id IN ({})".format(
            placeholders),
        list(paper_ids),
    ).fetchall()
    by_id = {r["id"]: r["pdf_path"] for r in rows}
    missing = [pid for pid in paper_ids if pid not in by_id]
    if missing:
        raise ValueError(
            "get_pdf_texts: unknown ID(s) {}".format(missing))

    out: list[dict] = []
    for pid in paper_ids:
        pdf_path = by_id[pid]
        if sidecar.is_ghost_path(pdf_path):
            out.append(asdict(PdfText(
                paper_id=pid, page_count=None,
                page_from=page_from, page_to=page_to,
                text="", truncated=False,
                error="ghost entry — no PDF available")))
            continue
        pc = pdf_text.page_count(pdf_path)
        text, truncated, err = pdf_text.extract_pages(
            pdf_path, page_from=page_from,
            page_to=page_to, max_chars=max_chars)
        out.append(asdict(PdfText(
            paper_id=pid, page_count=pc,
            page_from=page_from, page_to=page_to,
            text=text, truncated=truncated, error=err)))
    return out
