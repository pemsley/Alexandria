"""macOS AppleDouble files (`._foo.pdf`) are resource-fork metadata,
never real PDFs — a Mac copying into the library creates them and
they must be invisible to both the tree importer and the watcher."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alexandria import importer, watcher


def test_find_pdfs_skips_appledouble(tmp_path):
    for name in ("real.pdf", "._real.pdf", "sub/other.pdf",
                 "sub/._other.pdf"):
        p = tmp_path / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"%PDF fake")
    found = sorted(os.path.basename(p)
                   for p in importer.find_pdfs(str(tmp_path)))
    assert found == ["other.pdf", "real.pdf"]


def test_watcher_is_pdf_rejects_appledouble():
    assert watcher._is_pdf("/lib/paper.pdf") is True
    assert watcher._is_pdf("/lib/._paper.pdf") is False
    assert watcher._is_pdf("/lib/sub/._paper.pdf") is False
    # A dot-underscore *inside* the name is not AppleDouble.
    assert watcher._is_pdf("/lib/weird._name.pdf") is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
