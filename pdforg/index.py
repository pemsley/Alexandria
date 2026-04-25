"""Local SQLite index — a regeneratable cache. The truth lives in sidecars.

DB lives on local disk (XDG state dir), never on NFS.
"""

import json
import os
import sqlite3
from datetime import date, timedelta

XDG_STATE = os.environ.get("XDG_STATE_HOME") or os.path.join(
    os.path.expanduser("~"), ".local", "state")
DEFAULT_DB_PATH = os.path.join(XDG_STATE, "pdforg", "library.db")


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS papers (
    id           INTEGER PRIMARY KEY,
    pdf_path     TEXT UNIQUE NOT NULL,
    sidecar_path TEXT NOT NULL,
    thumb_path   TEXT,
    title        TEXT,
    authors_json TEXT,
    year         INTEGER,
    doi          TEXT,
    journal      TEXT,
    tags_json    TEXT,
    added_date   TEXT,
    sidecar_mtime REAL,
    sha256       TEXT,
    citations    INTEGER,
    citations_source  TEXT,
    citations_fetched TEXT,
    mark         TEXT,
    auto_keywords_json TEXT
);
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_papers_year      ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_doi       ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_sha256    ON papers(sha256);
CREATE INDEX IF NOT EXISTS idx_papers_cit_fetch ON papers(citations_fetched);
"""


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)")}
    if "sha256" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN sha256 TEXT")
    if "citations" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN citations INTEGER")
    if "citations_source" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN citations_source TEXT")
    if "citations_fetched" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN citations_fetched TEXT")
    if "mark" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN mark TEXT")
    if "auto_keywords_json" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN auto_keywords_json TEXT")
    conn.commit()


def open_db(path=DEFAULT_DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # check_same_thread=False because the GUI shares this connection with
    # background import / citation-refresh threads. SQLite itself
    # serialises access; WAL handles reader-writer concurrency.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(CREATE_TABLE)
    _migrate(conn)
    conn.executescript(CREATE_INDEXES)
    return conn


def normalize_doi(doi):
    if not doi:
        return None
    s = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.strip() or None


def find_duplicate(conn, doi=None, sha256=None, exclude_path=None):
    """Return an existing row matching this DOI or SHA-256, or None.
    DOI matching is case-insensitive."""
    if doi:
        ndoi = normalize_doi(doi)
        if ndoi:
            cur = conn.execute(
                "SELECT * FROM papers WHERE LOWER(doi)=? AND pdf_path<>?",
                (ndoi, exclude_path or ""))
            row = cur.fetchone()
            if row:
                return dict(row)
    if sha256:
        cur = conn.execute(
            "SELECT * FROM papers WHERE sha256=? AND pdf_path<>?",
            (sha256, exclude_path or ""))
        row = cur.fetchone()
        if row:
            return dict(row)
    return None


def upsert(conn, pdf_path, sidecar_path, thumb_path, record, sidecar_mtime):
    authors_json = json.dumps(record.get("authors") or [])
    tags_json = json.dumps(record.get("tags") or [])
    auto_keywords_json = json.dumps(record.get("auto_keywords") or [])
    conn.execute("""
        INSERT INTO papers
            (pdf_path, sidecar_path, thumb_path, title, authors_json,
             year, doi, journal, tags_json, added_date, sidecar_mtime, sha256,
             citations, citations_source, citations_fetched, mark,
             auto_keywords_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pdf_path) DO UPDATE SET
            sidecar_path=excluded.sidecar_path,
            thumb_path=excluded.thumb_path,
            title=excluded.title,
            authors_json=excluded.authors_json,
            year=excluded.year,
            doi=excluded.doi,
            journal=excluded.journal,
            tags_json=excluded.tags_json,
            added_date=excluded.added_date,
            sidecar_mtime=excluded.sidecar_mtime,
            sha256=excluded.sha256,
            citations=excluded.citations,
            citations_source=excluded.citations_source,
            citations_fetched=excluded.citations_fetched,
            mark=excluded.mark,
            auto_keywords_json=excluded.auto_keywords_json
    """, (pdf_path, sidecar_path, thumb_path,
          record.get("title"), authors_json,
          record.get("year"), record.get("doi"), record.get("journal"),
          tags_json, record.get("added_date"), sidecar_mtime,
          record.get("sha256"),
          record.get("citations"),
          record.get("citations_source"),
          record.get("citations_fetched"),
          record.get("mark"),
          auto_keywords_json))
    conn.commit()


def stale_citation_rows(conn, max_age_days=30, limit=None):
    """Rows whose citation count is missing or older than max_age_days,
    oldest first (NULL fetched dates come first). Used by the background
    refresh loop in the browser."""
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    sql = """
        SELECT * FROM papers
        WHERE doi IS NOT NULL AND doi <> ''
          AND (citations_fetched IS NULL OR citations_fetched < ?)
        ORDER BY (citations_fetched IS NULL) DESC, citations_fetched ASC
    """
    params = [cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_citations(conn, pdf_path, count, source, fetched_iso):
    conn.execute("""
        UPDATE papers
        SET citations=?, citations_source=?, citations_fetched=?
        WHERE pdf_path=?
    """, (count, source, fetched_iso, pdf_path))
    conn.commit()


def search(conn, query=None, limit=500):
    """Substring search across title/authors/doi/journal. None = list all."""
    if not query:
        cur = conn.execute(
            "SELECT * FROM papers ORDER BY year DESC, title LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]
    pat = "%" + query + "%"
    cur = conn.execute("""
        SELECT * FROM papers
        WHERE title LIKE ? OR authors_json LIKE ? OR doi LIKE ? OR journal LIKE ?
        ORDER BY year DESC, title
        LIMIT ?
    """, (pat, pat, pat, pat, limit))
    return [dict(r) for r in cur.fetchall()]


def all_pdf_paths(conn):
    cur = conn.execute("SELECT pdf_path FROM papers")
    return [r[0] for r in cur.fetchall()]


def remove(conn, pdf_path):
    conn.execute("DELETE FROM papers WHERE pdf_path=?", (pdf_path,))
    conn.commit()
