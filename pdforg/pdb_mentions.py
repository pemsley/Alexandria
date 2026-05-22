"""PDB-accession-code mention indexing.

Identifies the PDB IDs each library paper mentions (EuropePMC
annotations first, validated local-regex fallback second) and stores
them for cheap paper->PDB and PDB->paper queries. All network work is
best-effort and never fatal. See PDB_MENTIONS_BRIEF.md.
"""

import re
import shutil
import subprocess
import urllib.parse
import urllib.request
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


def get_valid_pdb_ids(conn):
    return {r["pdb_id"] for r in
            conn.execute("SELECT pdb_id FROM pdb_id_cache")}


def _store_valid_pdb_ids(conn, ids):
    now = _now_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO pdb_id_cache (pdb_id, fetched) VALUES (?, ?)",
        [(i.lower(), now) for i in ids if i])
    conn.commit()


def _valid_cache_is_stale(conn, max_age_days=7):
    row = conn.execute(
        "SELECT MAX(fetched) AS f FROM pdb_id_cache").fetchone()
    if not row or not row["f"]:
        return True
    try:
        last = datetime.fromisoformat(row["f"])
    except ValueError:
        return True
    age = datetime.now(timezone.utc) - last
    return age.days >= max_age_days


_EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"


def fetch_pmid_for_doi(doi, timeout=15):
    """Resolve DOI->PMID via EuropePMC search; None if no PMID."""
    if not doi:
        return None
    url = (_EPMC + "/search?query=DOI:" + urllib.parse.quote(doi, safe="")
           + "&format=JSON&resultType=lite")
    data = metrics._http_get_json(
        url, headers={"User-Agent": metrics.EUROPEPMC_UA,
                      "Accept": "application/json"}, timeout=timeout)
    return parse_pmid_from_search(data)


def fetch_europepmc_annotations(pmids, timeout=30):
    """Batched annotationsByArticleIds (<=8 ids/call). Returns
    [(pmid, pdb_id, section)]."""
    out = []
    pmids = [p for p in pmids if p]
    for i in range(0, len(pmids), 8):
        batch = pmids[i:i + 8]
        ids = ",".join("MED:" + p for p in batch)
        url = (_EPMC + "/annotations_api/annotationsByArticleIds?articleIds="
               + urllib.parse.quote(ids, safe=":,")
               + "&type=Accession%20Numbers&format=JSON")
        data = metrics._http_get_json(
            url, headers={"User-Agent": metrics.EUROPEPMC_UA,
                          "Accept": "application/json"}, timeout=timeout)
        out.extend(parse_europepmc_annotations(data))
    return out


def refresh_valid_pdb_id_cache(conn, max_age_days=7, timeout=60):
    """Refresh the local set of valid PDB ids from wwPDB's entries.idx
    when stale. Best-effort: leaves the cache untouched on failure."""
    if not _valid_cache_is_stale(conn, max_age_days):
        return
    url = "https://files.wwpdb.org/pub/pdb/derived_data/index/entries.idx"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": metrics.EUROPEPMC_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception as e:
        print("[pdb_mentions] valid-id refresh failed:", e)
        return
    ids = []
    for line in text.splitlines()[2:]:   # skip 2 header lines
        tok = line.split("\t", 1)[0].strip()
        if len(tok) == 4:
            ids.append(tok)
    if ids:
        _store_valid_pdb_ids(conn, ids)


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


def _pdf_fulltext(pdf_path):
    """All-pages plain text via pdftotext, or '' on any failure."""
    if not pdf_path or not shutil.which("pdftotext"):
        return ""
    try:
        proc = subprocess.run(
            ["pdftotext", pdf_path, "-"],
            capture_output=True, text=True, timeout=60)
        return proc.stdout or ""
    except Exception:
        return ""


def index_pdb_mentions_for_paper(conn, paper_id, pdf_path=None, doi=None):
    """Populate pdb_mentions for one paper. EuropePMC first; on no hits
    and available PDF text, validated local-regex fallback. Returns the
    number of mentions stored. Never raises on network failure."""
    row = conn.execute(
        "SELECT pdf_path, doi FROM papers WHERE id = ?", (paper_id,)
    ).fetchone()
    if row is None:
        return 0
    pdf_path = pdf_path or row["pdf_path"]
    doi = doi or row["doi"]

    pmid = None
    if doi:
        cached, val = _get_cached_pmid(conn, doi)
        if cached:
            pmid = val
        else:
            pmid = fetch_pmid_for_doi(doi)
            _cache_pmid(conn, doi, pmid)
    if pmid:
        hits = fetch_europepmc_annotations([pmid])
        mentions = [(pdb, sect) for (_pm, pdb, sect) in hits]
        if mentions:
            store_mentions(conn, paper_id, mentions, source="europepmc")
            return len(set(m[0] for m in mentions))

    text = _pdf_fulltext(pdf_path)
    if not text:
        return 0
    refresh_valid_pdb_id_cache(conn)
    valid = get_valid_pdb_ids(conn)
    ids = extract_pdb_ids_from_text(text, valid)
    if not ids:
        return 0
    store_mentions(conn, paper_id, [(i, None) for i in ids],
                   source="local_regex")
    return len(ids)
