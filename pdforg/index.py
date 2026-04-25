"""Local SQLite index — a regeneratable cache. The truth lives in sidecars.

DB lives on local disk (XDG state dir), never on NFS.
"""

import json
import os
import re
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
    auto_keywords_json TEXT,
    abstract     TEXT,
    first_author TEXT,
    last_author  TEXT,
    authorships_json TEXT,
    citations_by_year_json TEXT,
    published_version_json TEXT
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
    if "abstract" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN abstract TEXT")
    if "first_author" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN first_author TEXT")
    if "last_author" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN last_author TEXT")
    if "authorships_json" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN authorships_json TEXT")
    if "citations_by_year_json" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN citations_by_year_json TEXT")
    if "published_version_json" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN published_version_json TEXT")
    conn.commit()


_FTS_SCHEMA = """
CREATE VIRTUAL TABLE papers_fts USING fts5(
    title, authors, journal, abstract, keywords, doi,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER papers_fts_ai AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, authors, journal, abstract, keywords, doi)
    VALUES (new.id, new.title, new.authors_json, new.journal, new.abstract,
            new.auto_keywords_json, new.doi);
END;
CREATE TRIGGER papers_fts_ad AFTER DELETE ON papers BEGIN
    DELETE FROM papers_fts WHERE rowid = old.id;
END;
CREATE TRIGGER papers_fts_au AFTER UPDATE ON papers BEGIN
    DELETE FROM papers_fts WHERE rowid = old.id;
    INSERT INTO papers_fts(rowid, title, authors, journal, abstract, keywords, doi)
    VALUES (new.id, new.title, new.authors_json, new.journal, new.abstract,
            new.auto_keywords_json, new.doi);
END;
"""


def _ensure_fts(conn):
    """Create the FTS5 virtual table + sync triggers if absent, and
    backfill from existing rows. If the existing FTS table is the
    (now-deprecated) external-content variant, drop and recreate."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='papers_fts'"
    ).fetchone()
    if row:
        sql = (row[0] or "").lower()
        if "content=" not in sql:
            return  # already on the current schema
        # Migrate: drop the broken external-content table + its triggers.
        for trig in ("papers_fts_ai", "papers_fts_ad", "papers_fts_au"):
            conn.execute("DROP TRIGGER IF EXISTS {}".format(trig))
        conn.execute("DROP TABLE papers_fts")
        conn.commit()

    conn.executescript(_FTS_SCHEMA)
    conn.execute("""
        INSERT INTO papers_fts(rowid, title, authors, journal, abstract, keywords, doi)
        SELECT id, title, authors_json, journal, abstract, auto_keywords_json, doi
        FROM papers
    """)
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
    _ensure_fts(conn)
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


def _derive_first_last_author(record):
    """Pick first/last author names from the structured authorships if
    available, else fall back to the flat authors list."""
    authorships = record.get("authorships") or []
    first = last = None
    for a in authorships:
        pos = (a.get("position") or "").lower()
        name = a.get("name")
        if name:
            if pos == "first" and not first:
                first = name
            if pos == "last":
                last = name
    if first is None or last is None:
        flat = [a for a in (record.get("authors") or []) if a]
        if flat:
            if first is None:
                first = flat[0]
            if last is None:
                last = flat[-1] if len(flat) > 1 else flat[0]
    return first, last


def upsert(conn, pdf_path, sidecar_path, thumb_path, record, sidecar_mtime):
    authors_json = json.dumps(record.get("authors") or [])
    tags_json = json.dumps(record.get("tags") or [])
    auto_keywords_json = json.dumps(record.get("auto_keywords") or [])
    authorships_json = json.dumps(record.get("authorships") or [])
    cby_json = json.dumps(record.get("citations_by_year") or [])
    pv = record.get("published_version")
    pv_json = json.dumps(pv) if pv else None
    first_author, last_author = _derive_first_last_author(record)
    conn.execute("""
        INSERT INTO papers
            (pdf_path, sidecar_path, thumb_path, title, authors_json,
             year, doi, journal, tags_json, added_date, sidecar_mtime, sha256,
             citations, citations_source, citations_fetched, mark,
             auto_keywords_json, abstract,
             first_author, last_author, authorships_json,
             citations_by_year_json, published_version_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            auto_keywords_json=excluded.auto_keywords_json,
            abstract=excluded.abstract,
            first_author=excluded.first_author,
            last_author=excluded.last_author,
            authorships_json=excluded.authorships_json,
            citations_by_year_json=excluded.citations_by_year_json,
            published_version_json=excluded.published_version_json
    """, (pdf_path, sidecar_path, thumb_path,
          record.get("title"), authors_json,
          record.get("year"), record.get("doi"), record.get("journal"),
          tags_json, record.get("added_date"), sidecar_mtime,
          record.get("sha256"),
          record.get("citations"),
          record.get("citations_source"),
          record.get("citations_fetched"),
          record.get("mark"),
          auto_keywords_json,
          record.get("abstract"),
          first_author, last_author, authorships_json,
          cby_json, pv_json))
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


def _make_fts_query(query):
    """Convert a free-text search box query into an FTS5 MATCH expression
    with implicit prefix matching: 'Cro Will' → 'Cro* Will*'.
    Returns None if no usable tokens."""
    tokens = re.findall(r"\w+", query, re.UNICODE)
    if not tokens:
        return None
    return " ".join(t + "*" for t in tokens)


MARK_FILTER_NONE = "_none"   # sentinel for "unmarked papers only"


def _mark_filter_clause(mark_filter):
    """Return (sql_fragment, params) for a mark filter applied to the
    `papers` table. mark_filter is None (no filter), "_none" (only
    unmarked), or one of "red"/"orange"/"green"."""
    if mark_filter is None:
        return "", []
    if mark_filter == MARK_FILTER_NONE:
        return " AND (papers.mark IS NULL OR papers.mark = '')", []
    if mark_filter in ("red", "orange", "green", "cyan"):
        return " AND papers.mark = ?", [mark_filter]
    return "", []  # ignore unknown values


def search(conn, query=None, limit=500, mark_filter=None):
    """Search across title/authors/abstract/keywords/journal/doi via FTS5.

    Implicit prefix matching: each query token is treated as a prefix,
    so 'Cro' matches 'Croll' / 'Crowfoot' / 'crystallography'.

    `mark_filter` constrains by the user "Mark" colour.

    Result order: most recently *added* first (date the file was first
    imported, descending), then by sidecar mtime as a tie-break so a
    single-day import batch keeps stable order, then title. This puts a
    just-imported paper at row 0 — the user can find it without
    knowing the title."""
    mark_sql, mark_params = _mark_filter_clause(mark_filter)
    order_clause = (" ORDER BY added_date DESC, sidecar_mtime DESC, title")

    if not query:
        sql = ("SELECT papers.* FROM papers"
               " WHERE 1=1" + mark_sql + order_clause + " LIMIT ?")
        cur = conn.execute(sql, tuple(mark_params + [limit]))
        return [dict(r) for r in cur.fetchall()]

    fts_query = _make_fts_query(query)
    if fts_query:
        try:
            sql = ("SELECT papers.* FROM papers"
                   " JOIN papers_fts ON papers.id = papers_fts.rowid"
                   " WHERE papers_fts MATCH ?" + mark_sql +
                   order_clause + " LIMIT ?")
            cur = conn.execute(sql, tuple([fts_query] + mark_params + [limit]))
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            pass  # fall through to LIKE

    # LIKE fallback for unusual queries.
    pat = "%" + query + "%"
    sql = ("SELECT papers.* FROM papers"
           " WHERE (title LIKE ? OR authors_json LIKE ? OR doi LIKE ?"
           "        OR journal LIKE ? OR abstract LIKE ?"
           "        OR auto_keywords_json LIKE ?)" + mark_sql +
           order_clause + " LIMIT ?")
    cur = conn.execute(sql, tuple([pat]*6 + mark_params + [limit]))
    return [dict(r) for r in cur.fetchall()]


def all_pdf_paths(conn):
    cur = conn.execute("SELECT pdf_path FROM papers")
    return [r[0] for r in cur.fetchall()]


def remove(conn, pdf_path):
    conn.execute("DELETE FROM papers WHERE pdf_path=?", (pdf_path,))
    conn.commit()
