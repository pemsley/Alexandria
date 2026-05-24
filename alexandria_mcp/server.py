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

from alexandria import sidecar

from . import __version__, config, db
from .models import PaperDetail, PaperSummary


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
