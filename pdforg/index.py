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
DEFAULT_DB_PATH = os.path.join(XDG_STATE, "Alexandria", "library.db")


# Filesystem types we don't want SQLite's WAL file living on.
# Network filesystems break advisory locking (or do it unreliably),
# which corrupts the WAL eventually and tanks performance always.
# `nfs`/`nfs4` are by far the common case on shared-home setups
# (e.g. university login boxes); the others are belt-and-braces.
_NETWORK_FS_TYPES = {
    "nfs", "nfs4", "nfsd",
    "cifs", "smbfs", "smb2", "smb3",
    "fuse.sshfs", "sshfs",
}


def is_network_filesystem(path):
    """Return True if `path` (or its containing directory) lives on a
    networked filesystem where SQLite WAL is unsafe. Best-effort: on
    non-Linux platforms or if we can't read /proc/self/mountinfo, we
    return False (fail open — a missed warning is better than a
    bogus one)."""
    try:
        target = os.path.realpath(path)
    except OSError:
        return False
    try:
        with open("/proc/self/mountinfo", "r") as f:
            entries = f.readlines()
    except OSError:
        return False
    # mountinfo line layout (man 5 proc):
    #   36 35 98:0 /mnt1 /mnt2 rw,noatime - <fstype> <source> <opts>
    # The fstype is the field after the ' - ' separator. Pick the
    # mount with the longest mount point that is a prefix of `target`
    # — that's the most specific mount covering it.
    best = None
    best_len = -1
    for line in entries:
        sep = line.find(" - ")
        if sep < 0:
            continue
        before = line[:sep].split()
        after = line[sep + 3:].split()
        if len(before) < 5 or not after:
            continue
        mount_point = before[4]
        fstype = after[0]
        # Match mount_point as a path prefix, anchored at separator
        # so /home/foo doesn't accidentally match /home/foobar.
        if (target == mount_point
                or target.startswith(mount_point.rstrip("/") + "/")):
            if len(mount_point) > best_len:
                best = fstype
                best_len = len(mount_point)
    if best is None:
        return False
    return best.lower() in _NETWORK_FS_TYPES


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
    published_version_json TEXT,
    is_supplementary INTEGER DEFAULT 0
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
    if "is_supplementary" not in cols:
        conn.execute(
            "ALTER TABLE papers ADD COLUMN is_supplementary INTEGER DEFAULT 0")
    if "highlights_text" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN highlights_text TEXT")
    if "comments_text" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN comments_text TEXT")
    conn.commit()
    _reencode_unicode_columns(conn)
    _backfill_highlight_text(conn)


def _reencode_unicode_columns(conn):
    """One-time fix-up: rows written by older versions of the importer
    used `json.dumps(default ensure_ascii=True)`, escaping any non-
    ASCII character to `\\uXXXX`. The FTS5 unicode61 tokenizer can't
    see through that escape, so author surnames with diacritics
    (Müller, Casañal, Łukasz, ...) couldn't be searched.

    Walk every row, decode and re-encode the affected JSON columns
    with `ensure_ascii=False`. If any row needed rewriting, drop
    `papers_fts` so `_ensure_fts` rebuilds it from the cleaned rows."""
    affected_cols = ("authors_json", "auto_keywords_json",
                     "authorships_json", "tags_json",
                     "citations_by_year_json", "published_version_json")
    rows_changed = 0
    cur = conn.execute(
        "SELECT id, " + ", ".join(affected_cols) + " FROM papers")
    rows = cur.fetchall()
    for row in rows:
        updates = {}
        for col in affected_cols:
            v = row[col]
            if not v or "\\u" not in v:
                continue
            try:
                data = json.loads(v)
            except Exception:
                continue
            new = json.dumps(data, ensure_ascii=False)
            if new != v:
                updates[col] = new
        if updates:
            sets = ", ".join("{} = ?".format(c) for c in updates)
            params = list(updates.values()) + [row["id"]]
            conn.execute(
                "UPDATE papers SET " + sets + " WHERE id = ?", params)
            rows_changed += 1
    if rows_changed:
        # Force FTS rebuild from the cleaned rows. _ensure_fts() runs
        # right after _migrate() and will recreate from scratch when
        # it sees the table is gone.
        for trig in ("papers_fts_ai", "papers_fts_ad", "papers_fts_au"):
            conn.execute("DROP TRIGGER IF EXISTS {}".format(trig))
        conn.execute("DROP TABLE IF EXISTS papers_fts")
        conn.commit()
        print("index: re-encoded {} row(s) for Unicode-correct FTS; "
              "rebuilt papers_fts.".format(rows_changed))


def _highlights_blob(record):
    """Concatenate highlight selection text into a single string (or
    None). Used for FTS so 'find papers where I highlighted X' works."""
    parts = []
    for h in (record.get("highlights") or []):
        t = (h.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts) or None


def _comments_blob(record):
    """Concatenate comment text into a single string (or None). Each
    comment is a free-form note the user attached to a highlight."""
    parts = []
    for h in (record.get("highlights") or []):
        c = (h.get("comment") or "").strip()
        if c:
            parts.append(c)
    return "\n".join(parts) or None


def _backfill_highlight_text(conn):
    """One-time migration: walk each row whose highlights_text /
    comments_text is NULL, read the sidecar from disk, and populate
    the columns. Drops papers_fts so _ensure_fts() rebuilds it with
    the new content."""
    cur = conn.execute(
        "SELECT id, sidecar_path FROM papers"
        " WHERE highlights_text IS NULL AND comments_text IS NULL")
    rows = cur.fetchall()
    if not rows:
        return
    # Local import — sidecar imports index in some edge paths; keep
    # the dep one-directional at module-load time.
    from . import sidecar
    rows_changed = 0
    for row in rows:
        sc_path = row["sidecar_path"]
        if not sc_path or not os.path.isfile(sc_path):
            continue
        try:
            rec = sidecar.read(sc_path)
        except Exception:
            continue
        h_blob = _highlights_blob(rec)
        c_blob = _comments_blob(rec)
        if h_blob is None and c_blob is None:
            continue
        conn.execute(
            "UPDATE papers SET highlights_text = ?, comments_text = ?"
            " WHERE id = ?", (h_blob, c_blob, row["id"]))
        rows_changed += 1
    if rows_changed:
        for trig in ("papers_fts_ai", "papers_fts_ad", "papers_fts_au"):
            conn.execute("DROP TRIGGER IF EXISTS {}".format(trig))
        conn.execute("DROP TABLE IF EXISTS papers_fts")
        conn.commit()
        print("index: backfilled highlight/comment text for {} row(s); "
              "rebuilt papers_fts.".format(rows_changed))
    else:
        conn.commit()


_FTS_SCHEMA = """
CREATE VIRTUAL TABLE papers_fts USING fts5(
    title, authors, journal, abstract, keywords, doi,
    highlights, comments,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER papers_fts_ai AFTER INSERT ON papers BEGIN
    INSERT INTO papers_fts(rowid, title, authors, journal, abstract, keywords, doi,
                           highlights, comments)
    VALUES (new.id, new.title, new.authors_json, new.journal, new.abstract,
            new.auto_keywords_json, new.doi,
            new.highlights_text, new.comments_text);
END;
CREATE TRIGGER papers_fts_ad AFTER DELETE ON papers BEGIN
    DELETE FROM papers_fts WHERE rowid = old.id;
END;
CREATE TRIGGER papers_fts_au AFTER UPDATE ON papers BEGIN
    DELETE FROM papers_fts WHERE rowid = old.id;
    INSERT INTO papers_fts(rowid, title, authors, journal, abstract, keywords, doi,
                           highlights, comments)
    VALUES (new.id, new.title, new.authors_json, new.journal, new.abstract,
            new.auto_keywords_json, new.doi,
            new.highlights_text, new.comments_text);
END;
"""


def _ensure_fts(conn):
    """Create the FTS5 virtual table + sync triggers if absent, and
    backfill from existing rows. Recreates the table when the schema
    is out of date — this includes the legacy external-content variant
    and tables predating the highlights/comments FTS columns."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='papers_fts'"
    ).fetchone()
    if row:
        sql_lower = (row[0] or "").lower()
        on_current_schema = (
            "content=" not in sql_lower
            and "highlights" in sql_lower
            and "comments" in sql_lower)
        if on_current_schema:
            return
        for trig in ("papers_fts_ai", "papers_fts_ad", "papers_fts_au"):
            conn.execute("DROP TRIGGER IF EXISTS {}".format(trig))
        conn.execute("DROP TABLE papers_fts")
        conn.commit()

    conn.executescript(_FTS_SCHEMA)
    conn.execute("""
        INSERT INTO papers_fts(rowid, title, authors, journal, abstract, keywords, doi,
                               highlights, comments)
        SELECT id, title, authors_json, journal, abstract, auto_keywords_json, doi,
               highlights_text, comments_text
        FROM papers
    """)
    conn.commit()


CREATE_AUTHOR_SCORES = """
CREATE TABLE IF NOT EXISTS author_scores (
    openalex_id       TEXT PRIMARY KEY,
    self_excluded     INTEGER NOT NULL DEFAULT 1,
    software_total    INTEGER NOT NULL DEFAULT 0,
    software_n_citing INTEGER NOT NULL DEFAULT 0,
    software_n_works  INTEGER NOT NULL DEFAULT 0,
    method_total      INTEGER NOT NULL DEFAULT 0,
    method_n_citing   INTEGER NOT NULL DEFAULT 0,
    method_n_works    INTEGER NOT NULL DEFAULT 0,
    idea_total        INTEGER NOT NULL DEFAULT 0,
    idea_n_citing     INTEGER NOT NULL DEFAULT 0,
    idea_n_works      INTEGER NOT NULL DEFAULT 0,
    computed_at       TEXT NOT NULL
);
"""


# How long a cached citing-impact row is considered fresh. The
# underlying OpenAlex data moves on the order of weeks, so a month
# is plenty; we'd rather pay the ~3-min compute occasionally than
# hammer the API on every dialog open.
AUTHOR_SCORE_TTL_DAYS = 30


def get_author_score(conn, openalex_id):
    """Read the cached citing-impact result for `openalex_id`, or
    None if no row exists. Does *not* check freshness — callers
    decide whether to use the stale value or trigger a refresh."""
    if not openalex_id:
        return None
    row = conn.execute(
        "SELECT * FROM author_scores WHERE openalex_id = ?",
        (openalex_id,)).fetchone()
    if row is None:
        return None
    out = {"computed_at": row["computed_at"],
           "self_excluded": bool(row["self_excluded"])}
    for kind in ("software", "method", "idea"):
        total = row["{}_total".format(kind)]
        n_citing = row["{}_n_citing".format(kind)]
        n_works = row["{}_n_works".format(kind)]
        out[kind] = {
            "total": total,
            "n_citing": n_citing,
            "n_works": n_works,
            "mean": (total / n_citing) if n_citing else 0.0,
        }
    return out


def set_author_score(conn, openalex_id, result, self_excluded=True):
    """Persist a `compute_citing_impact` result for later reads.
    `result` is the dict it returns: per-bucket {total, n_citing,
    n_works} plus `computed_at`."""
    if not openalex_id or not result:
        return
    cols = ["openalex_id", "self_excluded", "computed_at"]
    vals = [openalex_id, 1 if self_excluded else 0,
            result.get("computed_at") or ""]
    for kind in ("software", "method", "idea"):
        b = result.get(kind) or {}
        cols += ["{}_total".format(kind),
                 "{}_n_citing".format(kind),
                 "{}_n_works".format(kind)]
        vals += [int(b.get("total") or 0),
                 int(b.get("n_citing") or 0),
                 int(b.get("n_works") or 0)]
    placeholders = ",".join(["?"] * len(cols))
    conn.execute(
        "INSERT OR REPLACE INTO author_scores ({}) VALUES ({})".format(
            ",".join(cols), placeholders),
        vals)
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
    conn.executescript(CREATE_AUTHOR_SCORES)
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
    # ensure_ascii=False so non-ASCII characters (Müller, Casañal,
    # Łukasz, ...) are stored literally rather than as \uXXXX escapes.
    # The FTS5 unicode61 tokenizer normalises diacritics on real
    # Unicode characters but can't see through JSON escapes — without
    # this, searching "Müller" returns 0 hits.
    authors_json = json.dumps(
        record.get("authors") or [], ensure_ascii=False)
    tags_json = json.dumps(
        record.get("tags") or [], ensure_ascii=False)
    auto_keywords_json = json.dumps(
        record.get("auto_keywords") or [], ensure_ascii=False)
    authorships_json = json.dumps(
        record.get("authorships") or [], ensure_ascii=False)
    cby_json = json.dumps(
        record.get("citations_by_year") or [], ensure_ascii=False)
    pv = record.get("published_version")
    pv_json = json.dumps(pv, ensure_ascii=False) if pv else None
    first_author, last_author = _derive_first_last_author(record)
    h_blob = _highlights_blob(record)
    c_blob = _comments_blob(record)
    conn.execute("""
        INSERT INTO papers
            (pdf_path, sidecar_path, thumb_path, title, authors_json,
             year, doi, journal, tags_json, added_date, sidecar_mtime, sha256,
             citations, citations_source, citations_fetched, mark,
             auto_keywords_json, abstract,
             first_author, last_author, authorships_json,
             citations_by_year_json, published_version_json,
             is_supplementary,
             highlights_text, comments_text)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            published_version_json=excluded.published_version_json,
            is_supplementary=excluded.is_supplementary,
            highlights_text=excluded.highlights_text,
            comments_text=excluded.comments_text
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
          cby_json, pv_json,
          1 if record.get("is_supplementary") else 0,
          h_blob, c_blob))
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


def stale_author_score_ids(conn, max_age_days=AUTHOR_SCORE_TTL_DAYS,
                           limit=None):
    """OpenAlex author IDs across the library that need a fresh
    `compute_citing_impact` run. Source: every `openalex_id` in
    every paper's `authorships_json`, left-joined against
    `author_scores`. Returns IDs that either have no cached row
    or whose cached row is older than max_age_days, oldest first
    (missing rows first)."""
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    # Collect distinct OpenAlex author IDs from authorships_json.
    # Stored shape: [{"name": ..., "openalex_id": "A...", "orcid": ...}, ...]
    ids = set()
    rows = conn.execute(
        "SELECT authorships_json FROM papers"
        " WHERE authorships_json IS NOT NULL").fetchall()
    for r in rows:
        try:
            ash = json.loads(r["authorships_json"])
        except Exception:
            continue
        for a in ash or []:
            oa = a.get("openalex_id") if isinstance(a, dict) else None
            if oa and isinstance(oa, str) and oa.startswith("A"):
                ids.add(oa)
    if not ids:
        return []
    # Bind into a temporary table for the join — avoids embedding
    # potentially thousands of IDs into a SQL IN clause.
    conn.execute("DROP TABLE IF EXISTS _author_score_candidates")
    conn.execute(
        "CREATE TEMP TABLE _author_score_candidates (openalex_id TEXT)")
    conn.executemany(
        "INSERT INTO _author_score_candidates (openalex_id) VALUES (?)",
        [(i,) for i in ids])
    sql = """
        SELECT c.openalex_id AS openalex_id, s.computed_at AS computed_at
        FROM _author_score_candidates c
        LEFT JOIN author_scores s ON s.openalex_id = c.openalex_id
        WHERE s.openalex_id IS NULL OR s.computed_at < ?
        ORDER BY (s.computed_at IS NULL) DESC, s.computed_at ASC
    """
    params = [cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    out = [r["openalex_id"] for r in conn.execute(sql, params).fetchall()]
    conn.execute("DROP TABLE IF EXISTS _author_score_candidates")
    return out


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


# Sort keys exposed in the UI. The value is the SQL expression used in
# ORDER BY (already including any COLLATE clause). Add new entries here
# and the dropdown in browse.py will pick them up via SORT_KEY_LABELS.
SORT_KEYS = {
    "added_date":   "added_date",
    "year":         "year",
    "title":        "title COLLATE NOCASE",
    "first_author": "first_author COLLATE NOCASE",
    "last_author":  "last_author COLLATE NOCASE",
    "citations":    "citations",
    "mark":         "mark",
}


def _order_clause(sort_key, sort_direction):
    """Return the ORDER BY clause (with leading space).

    The default — `added_date DESC` — preserves the import-flow
    ergonomics (newly-imported paper at row 0). Other keys put NULL/
    empty values at the end regardless of direction, then break ties
    on added_date DESC so a stable secondary order survives."""
    if sort_key not in SORT_KEYS:
        sort_key = "added_date"
    direction = (sort_direction or "DESC").upper()
    if direction not in ("ASC", "DESC"):
        direction = "DESC"

    if sort_key == "added_date":
        return " ORDER BY added_date {0}, sidecar_mtime {0}, title COLLATE NOCASE".format(
            direction)

    primary = SORT_KEYS[sort_key]
    if sort_key == "mark":
        nulls_last = "(papers.mark IS NULL OR papers.mark = '') ASC"
    else:
        nulls_last = "({} IS NULL) ASC".format(sort_key)
    return " ORDER BY {}, {} {}, added_date DESC, sidecar_mtime DESC".format(
        nulls_last, primary, direction)


def search(conn, query=None, limit=500, mark_filter=None,
           sort_key=None, sort_direction=None):
    """Search across title/authors/abstract/keywords/journal/doi via FTS5.

    Implicit prefix matching: each query token is treated as a prefix,
    so 'Cro' matches 'Croll' / 'Crowfoot' / 'crystallography'.

    `mark_filter` constrains by the user "Mark" colour.

    `sort_key` / `sort_direction` drive ORDER BY — see SORT_KEYS for
    valid keys. Default order is most recently *added* first, then by
    sidecar mtime as a tie-break, then title; that keeps a just-
    imported paper at row 0."""
    mark_sql, mark_params = _mark_filter_clause(mark_filter)
    order_clause = _order_clause(sort_key, sort_direction)

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
           "        OR auto_keywords_json LIKE ?"
           "        OR highlights_text LIKE ? OR comments_text LIKE ?)"
           + mark_sql + order_clause + " LIMIT ?")
    cur = conn.execute(sql, tuple([pat]*8 + mark_params + [limit]))
    return [dict(r) for r in cur.fetchall()]


def all_pdf_paths(conn):
    cur = conn.execute("SELECT pdf_path FROM papers")
    return [r[0] for r in cur.fetchall()]


def remove(conn, pdf_path):
    conn.execute("DELETE FROM papers WHERE pdf_path=?", (pdf_path,))
    conn.commit()
