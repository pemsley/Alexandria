"""MCP set_summary: a connected Claude client, having read the PDF
or JATS, writes its summary back into the paper's sidecar —
attributed with the model that wrote it, and marked as machine-
generated so it can never pass for the abstract."""

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


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A one-paper library with the MCP server pointed at it."""
    pdf = str(tmp_path / "paper.pdf")
    with open(pdf, "wb") as fh:
        fh.write(b"%PDF fake")
    sc = sidecar.sidecar_path_for(pdf)
    rec = sidecar.new_record(pdf)
    rec.update({"title": "A paper", "authors": ["A. Author"],
                "year": 2020, "doi": "10.1/x"})
    sidecar.write(sc, rec)
    db_path = str(tmp_path / "lib.db")
    conn = index.open_db(db_path)
    index.upsert(conn, pdf, sc, None, rec, os.path.getmtime(sc))
    paper_id = conn.execute(
        "SELECT id FROM papers").fetchone()[0]
    conn.close()

    monkeypatch.setattr(config, "db_path", lambda: db_path)
    monkeypatch.setattr(config, "library_root", lambda: str(tmp_path))
    monkeypatch.setattr(config, "readonly", lambda: False)
    # Connections are cached per-thread; drop any from earlier tests.
    if hasattr(db._tls, "ro_conn"):
        del db._tls.ro_conn
    yield {"paper_id": paper_id, "sidecar": sc, "pdf": pdf,
           "db_path": db_path}
    if hasattr(db._tls, "ro_conn"):
        del db._tls.ro_conn


def _call(**kw):
    fn = getattr(mcp_server.set_summary, "fn", mcp_server.set_summary)
    return fn(**kw)


def test_writes_summary_with_attribution(library):
    out = _call(paper_id=library["paper_id"],
                summary="A genetic algorithm docks ligands.",
                model="claude-opus-5", source="jats")
    assert out["status"] == "ok"
    rec = sidecar.read(library["sidecar"])
    s = rec["summary"]
    assert s["text"] == "A genetic algorithm docks ligands."
    assert s["model"] == "claude-opus-5"
    assert s["source"] == "jats"
    # ISO-8601 UTC stamp, recorded by the server not the client.
    assert s["generated_at"].endswith("Z")
    assert len(s["generated_at"]) >= 20


def test_overwrites_previous_summary(library):
    _call(paper_id=library["paper_id"], summary="First",
          model="claude-opus-5", source="pdf")
    _call(paper_id=library["paper_id"], summary="Second",
          model="claude-fable-5", source="jats")
    s = sidecar.read(library["sidecar"])["summary"]
    assert s["text"] == "Second"
    assert s["model"] == "claude-fable-5"


def test_preserves_other_sidecar_fields(library):
    _call(paper_id=library["paper_id"], summary="S",
          model="m", source="pdf")
    rec = sidecar.read(library["sidecar"])
    assert rec["title"] == "A paper"
    assert rec["doi"] == "10.1/x"
    assert rec["authors"] == ["A. Author"]


def test_rejects_unknown_paper(library):
    with pytest.raises(ValueError):
        _call(paper_id=999999, summary="S", model="m", source="pdf")


def test_rejects_empty_summary(library):
    with pytest.raises(ValueError):
        _call(paper_id=library["paper_id"], summary="   ",
              model="m", source="pdf")


def test_rejects_missing_model(library):
    with pytest.raises(ValueError):
        _call(paper_id=library["paper_id"], summary="S", model="",
              source="pdf")


def test_rejects_bad_source(library):
    with pytest.raises(ValueError):
        _call(paper_id=library["paper_id"], summary="S", model="m",
              source="vibes")


def test_rejects_overlong_summary(library):
    with pytest.raises(ValueError):
        _call(paper_id=library["paper_id"],
              summary="x" * (mcp_server.MAX_SUMMARY_CHARS + 1),
              model="m", source="pdf")


def test_refuses_in_readonly_mode(library, monkeypatch):
    monkeypatch.setattr(config, "readonly", lambda: True)
    with pytest.raises(ValueError):
        _call(paper_id=library["paper_id"], summary="S",
              model="m", source="pdf")


def test_index_row_refreshed_after_write(library):
    """The DB caches sidecar_mtime; a stale value would make the
    GUI's reconcile think the sidecar hadn't changed."""
    before = index.connect_existing(library["db_path"]).execute(
        "SELECT sidecar_mtime FROM papers WHERE id = ?",
        (library["paper_id"],)).fetchone()[0]
    _call(paper_id=library["paper_id"], summary="S", model="m",
          source="pdf")
    after = index.connect_existing(library["db_path"]).execute(
        "SELECT sidecar_mtime FROM papers WHERE id = ?",
        (library["paper_id"],)).fetchone()[0]
    assert after >= before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
