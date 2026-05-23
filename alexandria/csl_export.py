"""CSL JSON file export.

CSL JSON is the lingua franca of citation processors and the format
Zotero imports natively. The per-record conversion lives in
`csl.sidecar_to_csl`; this module just walks a row list, reads each
sidecar, and writes one JSON array out.
"""

import json
import os

from . import csl, sidecar


def export_rows_to_file(rows, output_path):
    """Write a CSL-JSON file from a list of index rows. Each row
    must expose `pdf_path` and `sidecar_path` (sqlite3.Row works).
    The sidecar — the canonical store — is read for each row.

    Returns `(written, skipped)`."""
    items = []
    skipped = 0
    seen_ids = set()
    for idx, row in enumerate(rows, start=1):
        try:
            sc_path = row["sidecar_path"]
        except (KeyError, IndexError, TypeError):
            sc_path = None
        if not sc_path or not os.path.isfile(sc_path):
            skipped += 1
            continue
        try:
            rec = sidecar.read(sc_path)
        except Exception as e:
            print("csl_export: cannot read {}: {}".format(sc_path, e))
            skipped += 1
            continue
        item = csl.sidecar_to_csl(rec)
        # CSL JSON IDs must be unique within the array — Zotero and
        # citeproc both key items by id and silently overwrite on
        # collision. sidecar_to_csl prefers DOI then bibtex_key then
        # the literal "item"; the last default trivially collides
        # across rows. Disambiguate any duplicate we see here.
        base_id = item.get("id") or "item"
        chosen = base_id
        if chosen in seen_ids:
            chosen = "{}-{}".format(base_id, idx)
            while chosen in seen_ids:
                idx += 1
                chosen = "{}-{}".format(base_id, idx)
        item["id"] = chosen
        seen_ids.add(chosen)
        items.append(item)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return len(items), skipped
