"""Local SQLite index — a regeneratable cache. The truth lives in sidecars.

DB lives on local disk (XDG state dir), never on NFS.
"""

import datetime
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import date, timedelta

XDG_STATE = os.environ.get("XDG_STATE_HOME") or os.path.join(
    os.path.expanduser("~"), ".local", "state")


def _stable_host_id():
    """Return a string that identifies this machine and is stable
    across reboots, network changes, OS updates, and DHCP lease
    renewals. Used by `_host_hash` to derive the per-host DB
    filename.

    First attempt was `socket.gethostname()` — it's not stable on
    macOS (the hostname changes with network membership, and the
    `*.local` mDNS name changes with the network's mDNS state).
    Without a stable identifier, the per-host DB filename shifts
    underneath the user and their library appears to "go missing"
    on a different network. The sources below are stable.

    Tried in order:
    1. `/etc/machine-id` (systemd) — host-specific even when $HOME
       is NFS-mounted; the right answer on modern Linux.
    2. `/var/lib/dbus/machine-id` — same, pre-systemd Linux.
    3. macOS `IOPlatformUUID` via `ioreg` — host-specific, set at
       hardware manufacturing time. Stable across OS reinstalls.
    4. Sentinel file at `$XDG_STATE_HOME/Alexandria/host-id` —
       random UUID generated on first launch. Caveat: when $HOME
       is NFS-mounted and shared, two hosts will read the *same*
       sentinel and compute the same hash, defeating the NFS
       protection. Acceptable fallback because the other sources
       cover the platforms (Linux + macOS) where NFS-shared $HOME
       is realistic."""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p, "r") as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5)
            for line in out.stdout.split("\n"):
                if "IOPlatformUUID" in line:
                    parts = line.split('"')
                    if len(parts) >= 4:
                        return parts[-2]
        except (OSError, subprocess.SubprocessError):
            pass
    sentinel = os.path.join(XDG_STATE, "Alexandria", "host-id")
    try:
        with open(sentinel, "r") as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    v = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "w") as f:
            f.write(v + "\n")
    except OSError:
        pass
    return v


def _host_hash():
    """4-character hex tag for this host, used in the per-host
    SQLite cache filename. See `_stable_host_id` for the
    identifier source."""
    return hashlib.blake2s(_stable_host_id().encode("utf-8"),
                           digest_size=2).hexdigest()


_HOST_DB_NAME = "library." + _host_hash() + ".db"
_LEGACY_DB_NAME = "library.db"
DEFAULT_DB_PATH = os.path.join(XDG_STATE, "Alexandria", _HOST_DB_NAME)

# Pattern for any `library.<hash>.db` filename — used by the
# migrator to spot stale-hash DBs left over from the brittle
# hostname-based design.
_HOSTHASH_DB_RE = re.compile(r"^library\.[0-9a-f]{4}\.db$")


def _migrate_legacy_db(host_db_path):
    """Adopt an existing legacy or stale-hash DB into the current
    stable-host-hashed filename. Runs in `open_db` before connect.

    Three cases:
    1. `library.<current-host-hash>.db` already exists → no-op.
    2. Only `library.db` (pre-host-hash era) exists → rename it.
    3. Exactly one `library.<other-hash>.db` exists (the host's
       previous identifier produced a different hash — most
       commonly because the early host-hash design used
       `socket.gethostname()` which isn't stable on macOS) →
       rename it.

    Multiple `library.<*>.db` candidates is ambiguous — we refuse
    to guess and log to stderr. The user has to move the one they
    want to `host_db_path` manually."""
    if os.path.exists(host_db_path):
        return
    parent = os.path.dirname(host_db_path)
    target_base = os.path.basename(host_db_path)
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    candidates = []
    for name in entries:
        if name == target_base:
            continue
        if name == _LEGACY_DB_NAME:
            candidates.append(name)
        elif _HOSTHASH_DB_RE.match(name):
            candidates.append(name)
    if not candidates:
        return
    if len(candidates) > 1:
        print(("Alexandria: multiple legacy DBs found in {} "
               "({}). Leaving them in place — the new DB will "
               "start empty. Move the one you want to {} "
               "manually.").format(parent,
                                   ", ".join(sorted(candidates)),
                                   target_base),
              file=sys.stderr)
        return
    src_base = candidates[0]
    for suffix in ("", "-wal", "-shm"):
        src = os.path.join(parent, src_base + suffix)
        dst = host_db_path + suffix
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.replace(src, dst)
            except OSError as e:
                print("legacy db rename {} → {} failed: {}"
                      .format(src, dst, e), file=sys.stderr)
    print("Alexandria: adopted {} as {}".format(src_base, target_base),
          file=sys.stderr)


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
    is_supplementary INTEGER DEFAULT 0,
    license_label TEXT,
    license_url   TEXT,
    crossmark_label    TEXT,
    crossmark_type     TEXT,
    crossmark_severity INTEGER,
    crossmark_doi      TEXT,
    crossmark_year     INTEGER,
    is_oa     INTEGER,
    oa_status TEXT
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
    if "license_label" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN license_label TEXT")
    if "license_url" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN license_url TEXT")
    if "crossmark_label" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN crossmark_label TEXT")
    if "crossmark_type" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN crossmark_type TEXT")
    if "crossmark_severity" not in cols:
        conn.execute(
            "ALTER TABLE papers ADD COLUMN crossmark_severity INTEGER")
    if "crossmark_doi" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN crossmark_doi TEXT")
    if "crossmark_year" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN crossmark_year INTEGER")
    if "is_oa" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN is_oa INTEGER")
    if "oa_status" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN oa_status TEXT")
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


CREATE_AUTHOR_WORKS_CACHE = """
CREATE TABLE IF NOT EXISTS author_works_cache (
    openalex_id  TEXT NOT NULL,
    sort_key     TEXT NOT NULL,
    works_json   TEXT NOT NULL,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (openalex_id, sort_key)
);
"""


# Days a cached works-list row is considered fresh. Shorter than
# `author_scores` because the most-recent ordering picks up new
# publications faster than citation tallies move.
AUTHOR_WORKS_TTL_DAYS = 7


def get_author_works_cache(conn, openalex_id, sort_key):
    """Return a cached works list as `{works, computed_at}` or None
    when there's no row. Freshness is the caller's call."""
    if not openalex_id or not sort_key:
        return None
    row = conn.execute(
        "SELECT works_json, computed_at FROM author_works_cache "
        "WHERE openalex_id = ? AND sort_key = ?",
        (openalex_id, sort_key)).fetchone()
    if row is None:
        return None
    try:
        works = json.loads(row["works_json"])
    except Exception:
        return None
    return {"works": works, "computed_at": row["computed_at"]}


def set_author_works_cache(conn, openalex_id, sort_key, works):
    """Persist a works list for (`openalex_id`, `sort_key`)."""
    if not openalex_id or not sort_key or works is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO author_works_cache "
        "(openalex_id, sort_key, works_json, computed_at) "
        "VALUES (?, ?, ?, ?)",
        (openalex_id, sort_key,
         json.dumps(works, ensure_ascii=False),
         date.today().isoformat()))
    conn.commit()


def clear_author_works_cache(conn, openalex_id):
    """Drop every cached sort for this author. Hook the refresh
    button up to this so the next fetch goes to OpenAlex."""
    if not openalex_id:
        return
    conn.execute(
        "DELETE FROM author_works_cache WHERE openalex_id = ?",
        (openalex_id,))
    conn.commit()


CREATE_SUBSCRIPTIONS = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    fetch_interval_hours INTEGER,
    last_fetched TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_kind
    ON subscriptions(kind);

CREATE TABLE IF NOT EXISTS discovered (
    id INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id)
        ON DELETE CASCADE,
    doi TEXT,
    openalex_id TEXT,
    title TEXT,
    authors_json TEXT,
    journal TEXT,
    year INTEGER,
    published_date TEXT,
    abstract TEXT,
    is_oa INTEGER DEFAULT 0,
    oa_url TEXT,
    oa_status TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(subscription_id, doi)
);
CREATE INDEX IF NOT EXISTS idx_discovered_fetched_at
    ON discovered(fetched_at);
CREATE INDEX IF NOT EXISTS idx_discovered_doi
    ON discovered(doi);
"""


CREATE_PDB_TABLES = """
CREATE TABLE IF NOT EXISTS pdb_mentions (
    paper_id  INTEGER NOT NULL,
    pdb_id    TEXT    NOT NULL,
    section   TEXT,
    source    TEXT    NOT NULL,
    fetched   TEXT    NOT NULL,
    PRIMARY KEY (paper_id, pdb_id, source),
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pdb_mentions_pdb   ON pdb_mentions(pdb_id);
CREATE INDEX IF NOT EXISTS idx_pdb_mentions_paper ON pdb_mentions(paper_id);

CREATE TABLE IF NOT EXISTS doi_pmid_cache (
    doi      TEXT PRIMARY KEY,
    pmid     TEXT,
    fetched  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdb_id_cache (
    pdb_id   TEXT PRIMARY KEY,
    fetched  TEXT NOT NULL
);
"""


def create_pdb_tables(conn):
    conn.executescript(CREATE_PDB_TABLES)


# Default cadence for the feed refresher when a subscription has
# no per-row override. 6 h matches Wispar's default and keeps us
# well inside CrossRef/OpenAlex polite-pool budgets even with many
# subscriptions.
FEED_FETCH_INTERVAL_HOURS = 6

# How long discovered rows live before getting pruned. Keeps the
# table from growing without bound; user has plenty of time to
# Get-PDF anything interesting.
DISCOVERED_RETENTION_DAYS = 60


def open_db(path=DEFAULT_DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _migrate_legacy_db(path)
    # check_same_thread=False because the GUI shares this connection with
    # background import / citation-refresh threads. SQLite itself
    # serialises access; WAL handles reader-writer concurrency.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # ON DELETE CASCADE on `discovered.subscription_id` only fires
    # when foreign-key enforcement is enabled — SQLite leaves this
    # off by default for backwards compatibility. Without this,
    # removing a subscription would leak its discovered rows.
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(CREATE_TABLE)
    _migrate(conn)
    conn.executescript(CREATE_INDEXES)
    conn.executescript(CREATE_AUTHOR_SCORES)
    conn.executescript(CREATE_AUTHOR_WORKS_CACHE)
    conn.executescript(CREATE_SUBSCRIPTIONS)
    create_pdb_tables(conn)
    _migrate_discovered(conn)
    _ensure_fts(conn)
    return conn


def _migrate_discovered(conn):
    """Additive ALTER for `discovered` so DBs created at the
    initial-commit schema get the columns we add later.
    `CREATE TABLE IF NOT EXISTS` is a no-op when the table is
    already there, so column additions need an explicit ALTER."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(discovered)")}
    if "oa_status" not in cols:
        conn.execute("ALTER TABLE discovered ADD COLUMN oa_status TEXT")
    conn.commit()


def migrate_sidecar_paths(conn, legacy_suffix=".meta.json",
                          new_suffix=".alexandria"):
    """Rewrite `papers.sidecar_path` from the legacy suffix to the
    new one. Companion to `sidecar.migrate_library_sidecars` — that
    one moves the files on disk; this one fixes the DB rows that
    still point at the old names. Idempotent: SQLite's REPLACE on a
    string that doesn't contain the legacy suffix is a no-op.
    Returns the number of rows updated."""
    cur = conn.execute(
        "UPDATE papers SET sidecar_path = REPLACE(sidecar_path, ?, ?) "
        "WHERE sidecar_path LIKE ?",
        (legacy_suffix, new_suffix, "%" + legacy_suffix))
    conn.commit()
    return cur.rowcount


# --- Subscriptions / discovered ---------------------------------------

def list_subscriptions(conn):
    """All subscriptions, newest first. Returns list of dict-like rows."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM subscriptions ORDER BY id DESC").fetchall()]


def add_subscription(conn, kind, name, query, fetch_interval_hours=None):
    """Create a new subscription. `kind` is one of
    'journal_issn' | 'openalex_query' | 'crossref_query'.
    `query` is kind-specific: comma-separated ISSNs for
    'journal_issn', the raw search string otherwise. Returns the
    new row id."""
    if kind not in ("journal_issn", "openalex_query", "crossref_query"):
        raise ValueError("unknown subscription kind: " + repr(kind))
    cur = conn.execute(
        "INSERT INTO subscriptions"
        " (kind, name, query, fetch_interval_hours, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, name, query, fetch_interval_hours,
         date.today().isoformat()))
    conn.commit()
    return cur.lastrowid


def remove_subscription(conn, subscription_id):
    """Drop a subscription. ON DELETE CASCADE removes its
    discovered rows too."""
    conn.execute(
        "DELETE FROM subscriptions WHERE id=?", (subscription_id,))
    conn.commit()


def mark_subscription_fetched(conn, subscription_id, when_iso=None):
    """Stamp the subscription as freshly refreshed. `when_iso`
    defaults to an ISO datetime (not just a date) so the
    staleness check in `stale_subscriptions` can compare against
    the current time at sub-day resolution."""
    conn.execute(
        "UPDATE subscriptions SET last_fetched=? WHERE id=?",
        (when_iso
         or datetime.datetime.now().isoformat(timespec="seconds"),
         subscription_id))
    conn.commit()


def stale_subscriptions(conn,
                        default_interval_hours=FEED_FETCH_INTERVAL_HOURS):
    """Subscriptions whose last_fetched is older than their
    fetch_interval_hours (or `default_interval_hours` when the
    column is NULL), or that have never been fetched. Returns
    list of dict rows, oldest-fetched first."""
    rows = conn.execute(
        "SELECT * FROM subscriptions ORDER BY"
        " (last_fetched IS NULL) DESC, last_fetched ASC").fetchall()
    out = []
    now = datetime.datetime.now()
    for r in rows:
        last = r["last_fetched"]
        interval_h = r["fetch_interval_hours"] or default_interval_hours
        if not last:
            out.append(dict(r))
            continue
        try:
            last_dt = datetime.datetime.fromisoformat(last)
        except ValueError:
            out.append(dict(r))
            continue
        if (now - last_dt).total_seconds() >= interval_h * 3600.0:
            out.append(dict(r))
    return out


def upsert_discovered(conn, subscription_id, article):
    """Insert a discovered article (a dict with doi/title/...) if
    we haven't seen this (subscription, doi) pair before. No-op
    when the article carries no DOI — the UNIQUE constraint needs
    something to dedup on and OpenAlex IDs alone aren't enough
    to fence the dup-detection across providers.

    Returns True if a row was inserted, False if it was a dup."""
    doi = article.get("doi")
    if not doi:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO discovered"
        " (subscription_id, doi, openalex_id, title, authors_json,"
        "  journal, year, published_date, abstract, is_oa, oa_url,"
        "  fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (subscription_id,
         doi.lower(),
         article.get("openalex_id"),
         article.get("title"),
         json.dumps(article.get("authors") or [],
                    ensure_ascii=False) if article.get("authors")
         else None,
         article.get("journal"),
         article.get("year"),
         article.get("published_date"),
         article.get("abstract"),
         1 if article.get("is_oa") else 0,
         article.get("oa_url"),
         datetime.datetime.now().isoformat(timespec="seconds")))
    return cur.rowcount > 0


def update_discovered_oa(conn, subscription_id, doi,
                         is_oa, oa_url, oa_status):
    """Patch the OA fields on a `discovered` row identified by
    `(subscription_id, doi)`. Used by the feed refresher after
    an Unpaywall enrichment lookup."""
    if not doi:
        return
    conn.execute(
        "UPDATE discovered SET is_oa=?, oa_url=?, oa_status=?"
        " WHERE subscription_id=? AND doi=?",
        (1 if is_oa else 0, oa_url, oa_status,
         subscription_id, doi.lower()))
    conn.commit()


def discovered_for(conn, subscription_id, limit=200):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM discovered WHERE subscription_id=?"
        " ORDER BY published_date DESC, fetched_at DESC LIMIT ?",
        (subscription_id, limit)).fetchall()]


def prune_old_discovered(conn,
                         retention_days=DISCOVERED_RETENTION_DAYS):
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=retention_days)).isoformat()
    cur = conn.execute(
        "DELETE FROM discovered WHERE fetched_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


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
    lic = record.get("license") or {}
    license_label = lic.get("label") if isinstance(lic, dict) else None
    license_url = lic.get("url") if isinstance(lic, dict) else None
    cm = record.get("crossmark") or {}
    if isinstance(cm, dict):
        crossmark_label = cm.get("label")
        crossmark_type = cm.get("type")
        crossmark_severity = cm.get("severity")
        crossmark_doi = cm.get("doi")
        crossmark_year = cm.get("year")
    else:
        crossmark_label = crossmark_type = None
        crossmark_severity = crossmark_doi = crossmark_year = None
    # OpenAlex OA flag — None when unknown, 0/1 otherwise. The chip
    # renderer differentiates "unknown" (no chip) from "known
    # paywalled" (no chip either, intentionally — the *absence* of
    # the OA chip is the paywalled signal).
    is_oa_raw = record.get("is_oa")
    if is_oa_raw is None:
        is_oa_int = None
    else:
        is_oa_int = 1 if is_oa_raw else 0
    oa_status_v = record.get("oa_status") or None
    conn.execute("""
        INSERT INTO papers
            (pdf_path, sidecar_path, thumb_path, title, authors_json,
             year, doi, journal, tags_json, added_date, sidecar_mtime, sha256,
             citations, citations_source, citations_fetched, mark,
             auto_keywords_json, abstract,
             first_author, last_author, authorships_json,
             citations_by_year_json, published_version_json,
             is_supplementary,
             highlights_text, comments_text,
             license_label, license_url,
             crossmark_label, crossmark_type, crossmark_severity,
             crossmark_doi, crossmark_year,
             is_oa, oa_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            comments_text=excluded.comments_text,
            license_label=excluded.license_label,
            license_url=excluded.license_url,
            crossmark_label=excluded.crossmark_label,
            crossmark_type=excluded.crossmark_type,
            crossmark_severity=excluded.crossmark_severity,
            crossmark_doi=excluded.crossmark_doi,
            crossmark_year=excluded.crossmark_year,
            is_oa=excluded.is_oa,
            oa_status=excluded.oa_status
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
          h_blob, c_blob,
          license_label, license_url,
          crossmark_label, crossmark_type, crossmark_severity,
          crossmark_doi, crossmark_year,
          is_oa_int, oa_status_v))
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


def rows_missing_crossref_extras(conn, limit=None):
    """Rows with a DOI but no cached license info. Drives the
    one-shot CrossRef-backfill pass in the browser — independent of
    the 30-day citation-refresh window. A single CrossRef call per
    row fills *both* license and crossmark, so we key off
    `license_label` and let crossmark come along for the ride
    (crossmark is often null even when license is set — most
    papers never get retracted)."""
    sql = """
        SELECT * FROM papers
        WHERE doi IS NOT NULL AND doi <> ''
          AND (license_label IS NULL OR license_label = '')
        ORDER BY added_date ASC
    """
    if limit:
        sql += " LIMIT {}".format(int(limit))
    return [dict(r) for r in conn.execute(sql).fetchall()]


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


def id_for_pdf_path(conn, pdf_path):
    row = conn.execute(
        "SELECT id FROM papers WHERE pdf_path = ?", (pdf_path,)).fetchone()
    return row["id"] if row else None
