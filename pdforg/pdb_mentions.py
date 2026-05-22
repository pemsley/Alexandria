"""PDB-accession-code mention indexing.

Identifies the PDB IDs each library paper mentions (EuropePMC
annotations first, validated local-regex fallback second) and stores
them for cheap paper->PDB and PDB->paper queries. All network work is
best-effort and never fatal. See PDB_MENTIONS_BRIEF.md.
"""

import re
from datetime import datetime, timezone

from . import index, metrics

# A PDB id is a digit 1-9 followed by three alphanumerics.
_PDB_RE = re.compile(r"\b([1-9][A-Za-z0-9]{3})\b")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_europepmc_annotations(payload):
    """Flatten an annotationsByArticleIds response to a list of
    (pmid, pdb_id_lower, section_lower_or_None) tuples, keeping only
    annotations whose source database tag is 'pdb' (case-insensitive)
    and whose accession matches the PDB id shape."""
    out = []
    for article in (payload or []):
        pmid = str(article.get("extId") or "").strip()
        if not pmid:
            continue
        for ann in (article.get("annotations") or []):
            is_pdb = any(
                (t.get("name") or "").strip().lower() == "pdb"
                for t in (ann.get("tags") or []))
            if not is_pdb:
                continue
            exact = (ann.get("exact") or "").strip()
            if not _PDB_RE.fullmatch(exact):
                continue
            section = ann.get("section")
            section = section.strip().lower() if section else None
            out.append((pmid, exact.lower(), section))
    return out


def parse_pmid_from_search(data):
    """Pull the first result's pmid from a EuropePMC search response,
    or None."""
    results = (((data or {}).get("resultList") or {}).get("result") or [])
    if not results:
        return None
    pmid = (results[0] or {}).get("pmid")
    pmid = str(pmid).strip() if pmid else ""
    return pmid or None


def _get_cached_pmid(conn, doi):
    """Return (cached: bool, pmid: str|None). cached=False means we've
    never looked this DOI up; cached=True with pmid=None means we
    looked and there is no PMID."""
    row = conn.execute(
        "SELECT pmid FROM doi_pmid_cache WHERE doi = ?", (doi,)).fetchone()
    if row is None:
        return (False, None)
    return (True, row["pmid"])


def _cache_pmid(conn, doi, pmid):
    conn.execute(
        "INSERT OR REPLACE INTO doi_pmid_cache (doi, pmid, fetched) "
        "VALUES (?, ?, ?)", (doi, pmid, _now_iso()))
    conn.commit()


def store_mentions(conn, paper_id, mentions, source):
    """mentions: iterable of (pdb_id_lower, section_or_None). Upserts
    (paper_id, pdb_id, source) rows; idempotent via the primary key."""
    now = _now_iso()
    for pdb_id, section in mentions:
        conn.execute(
            "INSERT OR REPLACE INTO pdb_mentions "
            "(paper_id, pdb_id, section, source, fetched) "
            "VALUES (?, ?, ?, ?, ?)",
            (paper_id, pdb_id.lower(), section, source, now))
    conn.commit()


def get_pdb_mentions(conn, paper_id):
    rows = conn.execute(
        "SELECT pdb_id, section, source, fetched FROM pdb_mentions "
        "WHERE paper_id = ? ORDER BY pdb_id", (paper_id,)).fetchall()
    return [dict(r) for r in rows]


def get_papers_for_pdb_id(conn, pdb_id):
    rows = conn.execute(
        "SELECT DISTINCT paper_id FROM pdb_mentions WHERE pdb_id = ? "
        "ORDER BY paper_id", (pdb_id.lower(),)).fetchall()
    return [r["paper_id"] for r in rows]


def extract_pdb_ids_from_text(text, valid_pdb_ids):
    """Return the set of lowercased PDB ids mentioned in `text` and
    present in `valid_pdb_ids` (a set of lowercased ids). Rejects
    all-digit and non-alphabetic candidates before validation."""
    if not text or not valid_pdb_ids:
        return set()
    out = set()
    for m in _PDB_RE.finditer(text):
        tok = m.group(1)
        if tok.isdigit():
            continue
        if not any(c.isalpha() for c in tok):
            continue
        low = tok.lower()
        if low in valid_pdb_ids:
            out.add(low)
    return out
