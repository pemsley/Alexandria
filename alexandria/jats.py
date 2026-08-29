"""Europe PMC JATS full-text fetch and store (BACKLOG: "Structured
full text (JATS) instead of PDF archaeology", v0).

The structured version of a paper, where one exists, lives beside
the PDF under the same-basename convention:

    paper.pdf  +  paper.pdf.alexandria  +  paper.pdf.jats.xml

Nothing else in the app reacts to the new extension (the watcher
and importer filter on `.pdf` / the sidecar suffix), so this module
is pure producer: resolve DOI → PMCID via the Europe PMC search
API, fetch `fullTextXML`, write it atomically. The outcome of each
attempt is recorded by the caller in the sidecar's `jats` block so
the backfill can skip papers already fetched and re-check absent
ones only after RECHECK_DAYS (coverage grows as embargoes lift).

Europe PMC needs no API key or registration.
"""

import datetime
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

JATS_SUFFIX = ".jats.xml"

# "no_pmcid" / "no_fulltext" answers are re-checked after this many
# days; "error" (transport trouble) is always retried; "stored" is
# final as long as the file is on disk.
RECHECK_DAYS = 30

_EPMC_ROOT = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_TIMEOUT = 30
_USER_AGENT = "Alexandria (https://github.com/pemsley/alexandria)"


def jats_path(pdf_path):
    return pdf_path + JATS_SUFFIX


def _today_iso():
    return datetime.date.today().isoformat()


def _get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_bytes(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()


def pmcid_for_doi(doi):
    """Resolve a DOI to a PMCID via the Europe PMC search API, or
    None when the paper has no PMC deposit. Transport errors
    propagate to the caller."""
    query = urllib.parse.quote('DOI:"{}"'.format(doi), safe="")
    url = ("{}/search?query={}&format=json&pageSize=1"
           .format(_EPMC_ROOT, query))
    payload = _get_json(url)
    results = (payload or {}).get(
        "resultList", {}).get("result", [])
    if not results:
        return None
    return results[0].get("pmcid") or None


def fetch_and_store(pdf_path, doi):
    """Try to fetch the JATS full text for `doi` and store it at
    jats_path(pdf_path). Returns the sidecar `jats` block:
    {status, pmcid, checked} with status one of
    stored | no_pmcid | no_fulltext | error."""
    checked = _today_iso()
    try:
        pmcid = pmcid_for_doi(doi)
    except Exception:
        return {"status": "error", "pmcid": None, "checked": checked}
    if not pmcid:
        return {"status": "no_pmcid", "pmcid": None, "checked": checked}
    try:
        data = _get_bytes("{}/{}/fullTextXML".format(_EPMC_ROOT, pmcid))
    except urllib.error.HTTPError as e:
        status = "no_fulltext" if e.code == 404 else "error"
        return {"status": status, "pmcid": pmcid, "checked": checked}
    except Exception:
        return {"status": "error", "pmcid": pmcid, "checked": checked}
    if not data:
        return {"status": "no_fulltext", "pmcid": pmcid,
                "checked": checked}
    dest = jats_path(pdf_path)
    fd, tmp = tempfile.mkstemp(
        suffix=".tmp", dir=os.path.dirname(dest) or ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return {"status": "error", "pmcid": pmcid, "checked": checked}
    return {"status": "stored", "pmcid": pmcid, "checked": checked}


def should_attempt(record, pdf_path):
    """Backfill skip logic: is a fetch attempt worthwhile for this
    paper, given its sidecar `record` and the files on disk?"""
    if os.path.isfile(jats_path(pdf_path)):
        return False
    block = record.get("jats") or {}
    status = block.get("status")
    if status in ("no_pmcid", "no_fulltext"):
        try:
            checked = datetime.date.fromisoformat(block.get("checked"))
        except (TypeError, ValueError):
            return True
        age = (datetime.date.today() - checked).days
        return age >= RECHECK_DAYS
    # Never tried, previous transport error, or "stored" with the
    # file gone from disk (the isfile gate above) — all worth a try.
    return True


def backfill(conn, on_progress=None, stop=None):
    """Walk every DOI-bearing paper, fetch-and-store JATS where an
    attempt is due, and record the outcome in each sidecar.

    on_progress(done, total) is called after each candidate
    (skipped ones included); `stop` is an optional threading.Event
    checked between papers. Returns
    {"stored": n, "absent": n, "errors": n, "skipped": n} —
    "absent" covers both no_pmcid and no_fulltext."""
    from . import index, sidecar

    rows = conn.execute(
        "SELECT pdf_path, sidecar_path, thumb_path FROM papers "
        "WHERE doi IS NOT NULL AND doi != ''").fetchall()
    totals = {"stored": 0, "absent": 0, "errors": 0, "skipped": 0}
    for done, (pdf_path, sc_path, thumb_path) in enumerate(rows, 1):
        if stop is not None and stop.is_set():
            break
        try:
            rec = sidecar.read(sc_path)
        except Exception:
            totals["errors"] += 1
            if on_progress:
                on_progress(done, len(rows))
            continue
        # JATS lives beside a real PDF — ghost rows (BibTeX imports
        # with 'bibtex:<key>' pseudo-paths, or rows whose PDF has
        # gone from disk) have nowhere to put it.
        if (not os.path.isfile(pdf_path)
                or not rec.get("doi")
                or not should_attempt(rec, pdf_path)):
            totals["skipped"] += 1
            if on_progress:
                on_progress(done, len(rows))
            continue
        block = fetch_and_store(pdf_path, rec["doi"])
        rec["jats"] = block
        sidecar.write(sc_path, rec)
        index.upsert(conn, pdf_path, sc_path, thumb_path, rec,
                     os.path.getmtime(sc_path))
        if block["status"] == "stored":
            totals["stored"] += 1
        elif block["status"] in ("no_pmcid", "no_fulltext"):
            totals["absent"] += 1
        else:
            totals["errors"] += 1
        if on_progress:
            on_progress(done, len(rows))
    return totals
