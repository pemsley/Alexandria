"""A connected client must be able to see which papers have
summaries — "how many of these have summaries?" was unanswerable:
the block lives in the sidecar, get_sidecars is capped at 50 IDs,
and nothing aggregated it.
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import index, sidecar

mcp_server = pytest.importorskip("alexandria_mcp.server")
from alexandria_mcp import config, db  # noqa: E402

AI = {"text": "machine text", "model": "claude-opus-5",
      "source": "jats", "generated_at": "2026-08-30T10:00:00Z"}
AI_PDF = dict(AI, source="pdf")
HUMAN = {"text": "my note", "author": "Paul Emsley",
         "generated_at": "2026-08-30T11:00:00Z"}


@pytest.fixture
def library(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lib.db")
    conn = index.open_db(db_path)
    ids = {}
    for name, summ in (("a", AI), ("b", AI_PDF), ("c", HUMAN),
                       ("d", None), ("e", None)):
        pdf = str(tmp_path / (name + ".pdf"))
        with open(pdf, "wb") as fh:
            fh.write(b"%PDF fake")
        sc = sidecar.sidecar_path_for(pdf)
        rec = sidecar.new_record(pdf)
        rec.update({"title": name.upper(), "doi": "10.1/" + name})
        if summ:
            rec["summary"] = summ
        sidecar.write(sc, rec)
        index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))
        ids[name] = conn.execute(
            "SELECT id FROM papers WHERE pdf_path = ?",
            (pdf,)).fetchone()[0]
    conn.close()
    monkeypatch.setattr(config, "db_path", lambda: db_path)
    monkeypatch.setattr(config, "library_root", lambda: str(tmp_path))
    monkeypatch.setattr(config, "readonly", lambda: False)
    if hasattr(db._tls, "ro_conn"):
        del db._tls.ro_conn
    yield ids
    if hasattr(db._tls, "ro_conn"):
        del db._tls.ro_conn


def _fn(name):
    tool = getattr(mcp_server, name)
    return getattr(tool, "fn", tool)


def test_overview_counts_the_library(library):
    out = _fn("summary_overview")()
    assert out["papers"] == 5
    assert out["with_summary"] == 3
    assert out["without_summary"] == 2
    assert out["machine_written"] == 2
    assert out["hand_written"] == 1


def test_overview_breaks_down_by_source_tier(library):
    out = _fn("summary_overview")()
    assert out["by_source"]["jats"] == 1
    assert out["by_source"]["pdf"] == 1


def test_overview_offers_ids_to_work_on(library):
    out = _fn("summary_overview")()
    assert sorted(out["missing_ids"]) == sorted(
        [library["d"], library["e"]])


def test_get_papers_reports_summary_presence(library):
    rows = _fn("get_papers")([library["a"], library["d"]])
    assert rows[0]["has_summary"] is True
    assert rows[0]["summary"]["model"] == "claude-opus-5"
    assert rows[1]["has_summary"] is False
    assert rows[1]["summary"] is None


def test_search_results_flag_summaries(library):
    rows = _fn("search_library")("A")
    assert rows, "expected the FTS search to find paper A"
    assert all("has_summary" in r for r in rows)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
