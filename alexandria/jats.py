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
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

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


# Structured-full-text siblings a paper can carry beside its PDF:
# (suffix, chip label, tooltip). The browser renders a small card
# chip per sibling present. The planned JATS->Markdown converter's
# output joins this list when it lands.
FULLTEXT_SIBLINGS = [
    (JATS_SUFFIX, "JATS",
     "Structured full text (JATS XML) stored beside the PDF"),
]


def fulltext_siblings(pdf_path):
    """(label, tooltip) for each structured-full-text file present
    beside `pdf_path`."""
    if not pdf_path:
        return []
    return [(label, tip)
            for suffix, label, tip in FULLTEXT_SIBLINGS
            if os.path.isfile(pdf_path + suffix)]


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


# --- Import by PubMed / PMC identifier -------------------------------

# PMCIDs always carry the prefix; PubMed IDs never do. That is what
# makes a bare number unambiguous — it can only be a PMID.
_PMCID_RE = re.compile(r"(?:pmcid\s*[:=]?\s*)?pmc[\s:]*?(\d{4,10})",
                       re.IGNORECASE)
_PMID_RE = re.compile(r"(?:pmid\s*[:=]?\s*)?(\d{4,9})\s*$",
                      re.IGNORECASE)


def parse_pubmed_identifier(text):
    """`("pmcid", "PMC…")`, `("pmid", "…")` or None.

    Accepts what a person actually pastes: a bare identifier with or
    without its prefix, and the URLs that PubMed, PMC and Europe PMC
    put in the address bar. Returns None for a DOI, so a caller can
    try DOI first and fall through to here."""
    s = (text or "").strip()
    if not s or "10." in s.split("/")[0] or s.lower().startswith("doi"):
        return None
    if "doi.org" in s.lower():
        return None
    # A URL: take the last meaningful path segment.
    if "://" in s:
        parts = [p for p in s.split("?")[0].split("#")[0].split("/") if p]
        s = parts[-1] if parts else ""
    m = _PMCID_RE.search(s)
    if m:
        return ("pmcid", "PMC" + m.group(1))
    m = _PMID_RE.match(s.strip())
    if m:
        return ("pmid", m.group(1))
    return None


def pubmed_query(kind, ident):
    """The Europe PMC search query for one identifier.

    Unquoted deliberately: `PMCID:"PMC5336473"` returns nothing while
    `PMCID:PMC5336473` works — the opposite of the `DOI:"..."` form
    `pmcid_for_doi` uses, and a silent no-result if "tidied"."""
    if kind == "pmcid":
        return "PMCID:{}".format(ident)
    return "EXT_ID:{} AND SRC:MED".format(ident)


def lookup_pubmed_identifier(text):
    """Resolve a pasted PubMed/PMC identifier to
    `{doi, pmid, pmcid, title, journal, year}`, or None.

    A PMC identifier means the full text is deposited and open, so a
    paper imported this way can also get its JATS and stands a good
    chance of an open-access PDF — see `fetch_and_store`."""
    parsed = parse_pubmed_identifier(text)
    if parsed is None:
        return None
    query = urllib.parse.quote(pubmed_query(*parsed), safe="")
    url = ("{}/search?query={}&format=json&pageSize=1&resultType=core"
           .format(_EPMC_ROOT, query))
    payload = _get_json(url)
    results = (payload or {}).get("resultList", {}).get("result", [])
    if not results:
        return None
    r = results[0]
    year = r.get("pubYear")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    return {"doi": r.get("doi") or None,
            "pmid": r.get("pmid") or None,
            "pmcid": r.get("pmcid") or None,
            "title": r.get("title") or None,
            "journal": r.get("journalTitle") or None,
            "year": year}


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


def _local(tag):
    """Strip any XML namespace from a tag name. Europe PMC serves
    some articles with a default JATS namespace and some without."""
    return tag.rsplit("}", 1)[-1]


def _find(elem, name):
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text_of(elem):
    """All text under `elem`, tags dropped, whitespace tidied — the
    JATS equivalent of what the reader sees."""
    if elem is None:
        return ""
    return " ".join("".join(elem.itertext()).split())


def _first_surname(ref):
    for child in ref.iter():
        if _local(child.tag) == "surname":
            return _text_of(child) or None
    return None


def _year_of(ref):
    for child in ref.iter():
        if _local(child.tag) == "year":
            txt = _text_of(child)
            digits = "".join(c for c in txt if c.isdigit())[:4]
            if len(digits) == 4:
                return int(digits)
    return None


# Publishers put a reference's DOI in three different places:
# <pub-id pub-id-type="doi"> (Springer/Nature), an
# <ext-link ext-link-type="doi"> whose xlink:href holds it (BMC/PMC
# — reading only <pub-id> lost every DOI on those papers), or
# nowhere but the printed citation string.
_DOI_IN_TEXT_RE = re.compile(
    r"\b(10\.\d{4,9}/[^\s\"<>,;)\]]+)", re.IGNORECASE)
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def _clean_doi(value):
    if not value:
        return None
    doi = str(value).strip().rstrip(".,;")
    return doi or None


def _doi_of(ref):
    for child in ref.iter():
        tag = _local(child.tag)
        if (tag == "pub-id"
                and (child.get("pub-id-type") or "").lower() == "doi"):
            got = _clean_doi(_text_of(child) or child.get(_XLINK_HREF))
            if got:
                return got
        if (tag == "ext-link"
                and (child.get("ext-link-type") or "").lower() == "doi"):
            got = _clean_doi(child.get(_XLINK_HREF) or _text_of(child))
            if got:
                return got
    m = _DOI_IN_TEXT_RE.search(_text_of(ref))
    return _clean_doi(m.group(1)) if m else None


def _tagged_text(ref, name):
    for child in ref.iter():
        if _local(child.tag) == name:
            return _text_of(child) or None
    return None


def parse_ref_list(xml_path):
    """Bibliography entries from a stored JATS file, in the shape the
    viewer's reference popover consumes:
    `{n, text, doi, surname, year, journal}`.

    This is the point of storing JATS. Everything
    `references_pdf.parse_bibliography` reconstructs from the PDF —
    where the reference section starts, which line begins entry 7,
    whether a number is a year or a page — the publisher marked up
    and then threw away when rendering. Here the entries are
    delimited, and the DOI is data rather than a search result.

    Returns [] for a missing, unreadable or reference-less file:
    callers fall back to parsing the PDF."""
    try:
        tree = ElementTree.parse(xml_path)
    except Exception:
        return []
    root = tree.getroot()
    out = []
    for elem in root.iter():
        if _local(elem.tag) != "ref":
            continue
        label = _tagged_text(elem, "label") or ""
        digits = "".join(c for c in label if c.isdigit())
        n = int(digits) if digits else len(out) + 1
        # <mixed-citation> is the publisher's own rendering of the
        # entry; prefer it over anything we reassemble.
        mixed = None
        for child in elem.iter():
            if _local(child.tag) == "mixed-citation":
                mixed = _text_of(child)
                break
        if mixed:
            text = mixed
        else:
            bits = [b for b in (
                _tagged_text(elem, "article-title"),
                _tagged_text(elem, "source"),
                str(_year_of(elem)) if _year_of(elem) else None)
                if b]
            surname = _first_surname(elem)
            if surname:
                bits.insert(0, surname)
            text = ". ".join(bits)
        out.append({
            "n": n,
            "text": text,
            "doi": _doi_of(elem),
            "surname": _first_surname(elem),
            "year": _year_of(elem),
            "journal": _tagged_text(elem, "source"),
        })
    return out


CONTEXT_CHARS = 60


def _ref_numbers_by_id(root):
    """{ref element id: reference number}. Numbered from <label>
    where present, else by position, matching parse_ref_list."""
    out = {}
    seen = 0
    for elem in root.iter():
        if _local(elem.tag) != "ref":
            continue
        seen += 1
        label = _tagged_text(elem, "label") or ""
        digits = "".join(c for c in label if c.isdigit())
        rid = elem.get("id")
        if rid:
            out[rid] = int(digits) if digits else seen
    return out


def parse_xrefs(xml_path):
    """In-text citation markers from a stored JATS file:
    `[{n, marker, context}, ...]` in document order.

    `context` is the prose immediately before the marker, which is
    what lets the caller find the citation in the PDF — some
    journals set citations as bare superscript numerals with no
    brackets and no link annotations, so there is nothing in the
    page text to recognise them by. Other markers inside the
    context are dropped, since the PDF renders those as superscripts
    too and they are not part of the prose.

    Only `ref-type="bibr"` markers count — figure and table
    cross-references are not citations — and a marker pointing at a
    reference we cannot number is discarded rather than guessed."""
    try:
        tree = ElementTree.parse(xml_path)
    except Exception:
        return []
    root = tree.getroot()
    numbers = _ref_numbers_by_id(root)
    if not numbers:
        return []

    # Walk the body in document order, keeping the running prose so
    # each marker knows the words in front of it.
    out = []
    prose = []

    def _walk(elem):
        for child in list(elem):
            tag = _local(child.tag)
            if tag == "xref":
                if (child.get("ref-type") or "").lower() == "bibr":
                    n = numbers.get(child.get("rid"))
                    if n is not None:
                        ctx = " ".join("".join(prose).split())
                        out.append({
                            "n": n,
                            "marker": _text_of(child),
                            "context": ctx[-CONTEXT_CHARS:],
                        })
                # The marker itself is not prose: skip its text so
                # it cannot end up inside a later context.
                if child.tail:
                    prose.append(child.tail)
                continue
            if child.text:
                prose.append(child.text)
            _walk(child)
            if child.tail:
                prose.append(child.tail)

    for elem in root.iter():
        if _local(elem.tag) == "body":
            if elem.text:
                prose.append(elem.text)
            _walk(elem)
            break
    return out


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
