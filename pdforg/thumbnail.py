"""First-page PNG thumbnails via pdftoppm (poppler-utils).

Avoids a Python dependency on pdf2image / PIL.
"""

import os
import subprocess


def make_thumbnail(pdf_path, out_path, width=240):
    """Render page 1 of pdf_path to out_path as a PNG at the given pixel width.
    Returns True on success."""
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return True
    tmp_root = out_path + ".tmp"
    try:
        # pdftoppm appends "-1.png" when -singlefile is NOT used; with
        # -singlefile, it appends only ".png".
        subprocess.run(
            ["pdftoppm", "-singlefile", "-png",
             "-scale-to-x", str(width), "-scale-to-y", "-1",
             "-f", "1", "-l", "1",
             pdf_path, tmp_root],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return False
    produced = tmp_root + ".png"
    if not os.path.isfile(produced):
        return False
    os.rename(produced, out_path)
    return True
