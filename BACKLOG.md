# pdforg Backlog

Pending features, roughly grouped. Newest at the top of each section.

## Top priority
- **Highlight text + attach comments** (poppler viewer): drag-select →
  highlight quads on page → optional comment popover. Highlights and
  comments persist; navigable from a per-paper sidebar; FTS-indexed.
- **Comment author identity:** for now we stamp comments with the OS
  username (`getpass.getuser()`). Add a Preferences entry so the user
  can set a display name (e.g. "BigDave") that's used instead. Migrate
  existing comments lazily.

## Import / ingestion
- **Import from DOI (paste):** small dialog with a DOI entry field. Fetch
  metadata via OpenAlex/CrossRef, then attempt to fetch the open-access
  PDF (when `is_oa` and `oa_url` are present); if no OA copy, save a
  metadata-only sidecar with a placeholder so it shows up in the index.
- **Import from BibTeX file:** use `bibtexparser` to parse a `.bib`
  file and create a sidecar per entry. Entries without a `file =
  {...}` PDF path become **PDF-less sidecars** — they show up in the
  browser as cards with a "Get PDF" action (try the OpenAlex `oa_url`,
  fall back to opening the DOI in a browser). Persist the BibTeX
  citation key in the sidecar (e.g. `"bibtex_key": "smith2024foo"`)
  so future re-exports preserve it. For entries with a DOI, enrich
  via OpenAlex.
- URL drops (drag a journal/PDF URL into the browser, fetch + import).

## Viewer (poppler) v2
- Text selection + hit-testing (cairo overlay → page coords) — done v1.
- Highlighting / annotation ("stabilo" mode) — done v1.
- **Better column-aware text selection.** v1 uses a heuristic
  (cluster line-start x positions, find the largest cluster gap as a
  column boundary). Works for typical 1- and 2-column papers,
  including MDPI's "left-margin metadata + right-column body" layout.
  **Known to fail on**: 3+ column layouts, sidebars / pull-quotes that
  share a y-band with body text, pages where column structure changes
  partway down, tables, figures-with-captions where the caption flows
  oddly. Possible upgrades, in order of cost: (a) fall back to strict
  bbox-clip when no clean cluster is detected; (b) bring in
  `pdfminer.six` or `pymupdf` for proper layout analysis; (c) implement
  page segmentation via connected-components on glyph rectangles.
- **References panel** (per-paper, popover). Use OpenAlex's
  `referenced_works` field on the paper's Work record — already
  structured (each is an OpenAlex Work ID), batched-resolve to
  title / authors / year / DOI / journal exactly like the Cited-by
  popover. Reuses `_build_related_row` for rendering and the
  green "✓ in library" chip for DOI matches. New helper
  `metrics.fetch_references(doi|openalex_id)` mirrors
  `fetch_cited_by`. No PDF parsing, no HTML scraping, no GROBID;
  works for any paper OpenAlex has data on (which is most).
- Page thumbnails sidebar.

## Discovery
- New-citations-of-favourites feed (papers citing my green-marked papers,
  filtered by overlap with my other green-marked papers' keywords)
- Show abstract somewhere visible (hover preview? expandable card?)
- Show ORCID / institution in the metadata editor

## Sorting & filtering
- Sort by date / journal / title / first-author / last-author
- Tag chips + filter sidebar
- FTS to include mark labels

## Export
- BibTeX export (single, filtered, whole library).
- **RIS export** (single, filtered, whole library). Same shape as
  BibTeX export but RIS — used by EndNote, RefWorks, Mendeley, Zotero
  and most journals' "Cite this paper" widgets. The data layer
  largely overlaps with BibTeX (we already capture title / authors /
  year / journal / doi / volume / number / pages / abstract /
  keywords). Tag mapping: `TY  - JOUR/CHAP/CONF/...`, `AU  - Last,
  First`, `TI  -`, `PY  -`, `JO  -` or `T2  -`, `DO  -`, `VL  -`,
  `IS  -`, `SP  -`/`EP  -`, `AB  -`, `KW  -`, `ER  - `. No external
  dep needed; trivial line-format. Round-trip-test the way we did
  for BibTeX.
- **"Cite this paper as…" — CSL formatting** via `citeproc-py`
  (BSD-2, 179 KB, lxml dep). Right-click a card → submenu with a
  few preferred styles (APA / Vancouver / Nature / Chicago) →
  formatted citation copied to clipboard. CSL JSON sits in the
  middle: sidecar record → CSL JSON → citeproc-py(style.csl) →
  formatted text. The companion `citeproc-py-styles` bundle has
  ~10k CSL style files (12 MB); we'd vendor only a handful and let
  users drop additional `.csl` from the Zotero CSL repository into
  a config dir.

## Watcher
- Recursive subdir watching (currently flat on `LIBRARY_ROOT`)

## Compatibility
- GTK4 < 4.10 fallback (some widgets we use are 4.10+)

## Speculative / maybe-never

These are ideas worth remembering but explicitly **not on the
roadmap**. Listed here so we don't forget to evaluate them later, not
as commitments. Each would need a fresh design discussion — and a
sober "do we actually want this?" — before any work happens.

- **Ask-the-library (RAG)** — "ask a question, get an answer cited
  from my own library". The `citeformer` PyPI package
  (`random-walks/citeformer`, Apache-2.0) does this kind of thing —
  forces an LLM to emit structurally-verifiable citation markers via
  constrained decoding. Heavy deps (httpx, lark, lxml, pydantic,
  pypdf, plus optional model-provider extras: anthropic / openai /
  google / llama-cpp / transformers). Adds an LLM dependency to
  Alexandria, which is currently a deliberately offline-first app.
  We may decide we don't want that at all.
