"""Per-PDF sidecar metadata file (the canonical store).

Lives next to the PDF as <pdf>.meta.json. Plain JSON, hand-editable,
survives any DB schema change.
"""

import json
import os
from datetime import date

SCHEMA_VERSION = 1
SIDECAR_SUFFIX = ".meta.json"


def sidecar_path_for(pdf_path):
    return pdf_path + SIDECAR_SUFFIX


def thumb_path_for(pdf_path):
    return pdf_path + ".thumb.png"


def new_record(pdf_path):
    return {
        "schema": SCHEMA_VERSION,
        "pdf_filename": os.path.basename(pdf_path),
        "title": None,
        "authors": [],
        "year": None,
        "doi": None,
        "journal": None,
        "tags": [],
        "notes": "",
        "mark": None,           # "red" / "orange" / "green" / None
        "hand_edited": False,
        "added_date": date.today().isoformat(),
        "sha256": None,
        "citations": None,
        "citations_source": None,
        "citations_fetched": None,
        "auto_keywords": [],
        "raw": {},
    }


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write(path, record):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
