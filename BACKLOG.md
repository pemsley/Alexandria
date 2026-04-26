# pdforg Backlog

Pending features, roughly grouped. Newest at the top of each section.

## Top priority

(Nothing pinned here right now — see sections below.)

## Import / ingestion
- **Drag-and-drop / CLI imports from outside the library tree
  (Flatpak).** Menu-driven Import Files / Import Folder now go
  through `importer.stage_into_library`, which copies the picked PDF
  into the library root before indexing — works inside Flatpak via
  the FileChooser portal's transient bind-mount, and outside Flatpak
  it consolidates everything under `~/Documents/Alexandria` instead
  of scattering sidecars to wherever the source PDF lived. Still
  open: drag-and-drop and any CLI-handed paths from outside the
  library tree don't go through the portal and will silently fail to
  read inside Flatpak. Need an explicit "this file is outside the
  sandbox" path — either re-prompt the user via `Gtk.FileDialog` to
  relaunder the path through the portal, or surface a clear error
  message.
- **Multi-directory libraries / multiple catalogs.** Currently
  `LIBRARY_ROOT` is a single directory. Some users want separate
  catalogs (e.g. "Personal", "Work", "Crystallography teaching")
  with independent watchers, sidecars, marks-config and ideally
  separate FTS indexes. Design notes:
    - Either *one* DB with a `catalog_id` column on every row, or
      one DB per catalog (cleaner isolation, simpler search, but
      more state directories).
    - UI: catalog switcher in the header, or open multiple browser
      windows each pinned to a catalog.
    - Cross-catalog search ("find this DOI anywhere") becomes a
      design question — do we union queries across catalogs, or
      keep search strictly per-catalog?
    - Sidecar paths and ghost-import dirs (`.alexandria-bibtex/`)
      are still per-catalog.
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
  *(Done: card popover + PDF-parsed fallback wired in.
  Citation→bibliography click-to-jump and the resolved-reference
  toolbar popover work for publisher-annotated PDFs (Springer,
  Nature). What's still missing is the parser-driven fallback for
  PDFs without those annotations — see "Citation hit-testing
  fallback" below.)*
- **Citation hit-testing fallback for un-annotated `[N]` PDFs
  (numbered).** Click-to-jump and the resolved-reference popover
  currently work on PDFs with publisher-embedded `/Link`
  annotations + named destinations (Springer's `:CR<N>:`,
  Nature's `bm_CR<N>`). PDFs without those — older /
  iText-reprocessed Wiley files — get the popover-fallback for
  the References panel but no in-page click-to-jump. Path B from
  the Step-2 design notes: walk each page's `get_text_layout()`
  rects, find `\[\d+\]` (and `[N-M]` ranges via
  `expand_citation_token`) in the body, find the `[N]` markers at
  the start of bibliography entries, synthesise the same `(rect,
  target_page, target_top, ref_n)` records
  `pdf_links.read_citation_links` produces, and pour them into
  `self.citation_links` only on pages where Path A produced
  nothing. The existing click handler / hover cursor / `_jump_to`
  / reference popover all keep working unchanged because they
  consume the index, not its source.
- **Author-year citation hit-testing (Acta Cryst, IUCr).**
  *Phase 1 done:* `references_pdf.parse_bibliography` now detects
  author-year style and returns entries with `surname` / `year` /
  `suffix` / `key` fields alongside `n`; the References popover
  renders them with a `(Sheldrick, 2008)` chip. *Phase 2 still
  open:* in-page click-to-jump from `(Surname, YYYY)` /
  `Surname (YYYY)` patterns. Walk each page's text layout, regex
  `\(([A-Z]\S+(?:\s+et\s+al\.)?,?\s+\d{4}[a-z]?(?:,?\s*\d{4}[a-z]?)*)\)`
  for parenthetical and `[A-Z]\S+(?:\s+et\s+al\.)?\s+\(\d{4}[a-z]?\)`
  for narrative form, look up `(surname_lower, year)` in the
  parsed bibliography (tolerating "et al." → first-author
  surname), synthesise the same `(rect, target_page, target_top,
  ref_key)` records `pdf_links.read_citation_links` produces but
  with `ref_key` repurposed as the canonical `key` string.
  Multi-cite tokens like `(Smith, 2020; Jones, 2003)` and
  `(Smith, 2020a, 2020b)` expand to multiple refs.
- Page thumbnails sidebar.

## Discovery
- **Author-awards chip on paper cards.** A small badge when an
  author is a recipient of a high-prestige scientific award. Two
  shipped tiers + a parked third:

    | Tier | Awards | Chip colour (light bg) | Cohort |
    |------|--------|------------------------|--------|
    | Nobel | Physics, Chemistry, Physiology or Medicine | gold `#b8860b` | ~600 living |
    | Field-defining | Lasker, Crafoord, Wolf, Fields Medal, Turing | deep purple `#5a2a82` | ~500 living |
    | (Optional) Society fellow | Royal Society, NAS, Académie des sciences, Leopoldina | muted teal `#2a7a7a` | tens of thousands |

  Design rules:
    - **One chip per paper, highest tier wins.** A laureate who's
      also an RS Fellow shows gold, not stacked. Avoids chip-soup
      on RS/NAS-heavy papers.
    - **Top two tiers ship by default.** The Society-fellow tier
      is too populous to be a useful signal alone — keep it off by
      default, toggleable in Preferences for users who care.
    - **Whole feature is gated by a Preference: "Show author award
      chips" (default on, off, or per-tier checkboxes).** Some
      users will find the chips delightful, some will find them
      noisy / hero-worship-y. Don't impose; let them choose.
    - No emoji — plain text chip styled like the existing Citation
      Classic markup (see memory: `feedback_no_color_emoji`).
    - Tooltip: "Nobel laureate &lt;name&gt; (&lt;year&gt;,
      &lt;category&gt;) is among the authors of this paper."

  Data plumbing:
    - OpenAlex doesn't carry the award field, but author records
      include `ids.wikidata` for well-known authors. Wikidata has
      `P166 (award received)` → award Q-IDs.
    - One-time fetch via SPARQL: "people with `P166` in the Nobel /
      Lasker / Wolf / Crafoord / Fields / Turing / RS / NAS /
      Académie / Leopoldina families" → static JSON asset shipped
      with the app, keyed by Wikidata ID, value `{tier, award,
      year, category}`. Refresh annually after the October Nobel
      announcement.
    - At paper render time: walk `authorships`, check each
      author's `wikidata` ID against the set; render the highest
      tier found.
- **Show an author's prior institutions in the author dialog.**
  OpenAlex's author record carries an `affiliations` array of
  `{institution: {display_name, ...}, years: [...]}` entries — a
  career timeline of every institution that author has published
  from. Surface this in the author-works window (`author_works.py`):
  a small list under the author header, sorted most-recent year
  first, showing institution name + year range. The list visible
  on a `curl /authors?search=...` for "Clyde A. Smith" gives a CV
  at a glance — Stanford SSRL (current), Notre Dame 2007–2010,
  Vermont 1998–2000, NIH 2023–2024, Penn State 2023–2024. Free
  data we already pay for; surprisingly informative as a "who is
  this person" cue when disambiguating.
- **Cache the Related-works *paper list*.** Cited-by and References
  are now cached in the sidecar (`cited_by_cache` / `references_cache`)
  with a manual refresh button in each popover; Related-works still
  re-queries OpenAlex on every open. Same approach: stash the list
  with a `fetched` timestamp and a refresh button. Optional follow-
  up: time-based invalidation (e.g. weekly), and a "refresh all
  caches" action somewhere central.
- New-citations-of-favourites feed (papers citing my green-marked papers,
  filtered by overlap with my other green-marked papers' keywords)
- Show abstract somewhere visible (hover preview? expandable card?)
- Show ORCID / institution in the metadata editor
- *(low priority)* **Heuristic detection of suspect-conflated
  OpenAlex author records.** When picking an author from the
  Discover dialog, a single OpenAlex record can quietly contain
  multiple real people — OpenAlex's author resolver sometimes
  collapses unrelated researchers under one ID. Symptoms we saw
  on a "James Smith" lookup: anachronistic affiliations
  ("Google (United States) 1942–1944" — Google didn't exist),
  topics from incompatible fields (telomeres + pesticide
  toxicity), affiliation gaps spanning 80+ years on a single
  person. None of these are individually decisive, but together
  they're a strong "this record is a chimera" signal. A small
  sanity check could surface a quiet warning chip on the author
  dialog ("⚠ likely conflates multiple researchers — verify
  before importing"). Heuristics worth combining:
    - Affiliation year before 1900 *or* after the OpenAlex
      record's earliest credible publication.
    - Two `topics[]` entries from far-apart concept trees
      (`topics[0].field.id` differs from `topics[1].field.id`).
    - Affiliation list spans > N years (e.g. > 60).
    - Single record claiming both an industry employer (Google,
      Pfizer, …) *and* a hospital *and* a university across
      overlapping years.
  Low priority because the user can usually spot the chimera
  themselves once they open the works window — but a quiet hint
  before they get there is friendlier.

## Sorting & filtering
- Tag chips + filter sidebar
- FTS to include mark labels
- Persist sort choice across sessions (currently session-only —
  default `added_date DESC` is restored on every launch).

## Export
- BibTeX export (single, filtered, whole library).
- **User-supplied CSL styles.** "Cite this paper as…" currently
  ships APA / Vancouver / Nature / Chicago author-date in
  `pdforg/styles/`. Add a Preferences entry that lets users drop
  additional `.csl` files into `~/.config/Alexandria/styles/` and
  have them picked up by `csl_format.list_styles()`.
- **CSL JSON file export.** `csl.sidecar_to_csl_array(rec)` already
  produces the right shape; just needs an "Export CSL JSON…" menu
  item parallel to BibTeX/RIS. Useful for Zotero import (which
  reads CSL JSON natively).

## Watcher
- Recursive subdir watching (currently flat on `LIBRARY_ROOT`)
- **Clean process shutdown on window close.** Symptom: closing
  the browser window often leaves the Python process alive in the
  background (`ps aux | grep pdforg-browse` shows zombies from
  earlier sessions). They keep file descriptors open on
  `~/.local/state/Alexandria/library.db`, which means the next
  launch of Alexandria fails to acquire a WAL lock and aborts
  with "disk I/O error" (now caught and surfaced as a friendly
  dialog, but the underlying problem still requires `pkill -f
  pdforg-browse` to recover). Likely cause: the
  `LibraryWatcher.GFileMonitor` thread or one of the background
  refresh threads (citation refresh, cited-by fetch, cache write)
  is keeping the GLib main loop / Python interpreter alive after
  the window's close-request fires. Fix candidates:
    - Wire `BrowserWindow._on_close_request` to explicitly
      `watcher.stop()` and join any short-lived background threads
      with a small timeout before returning False.
    - Mark all worker threads as `daemon=True` (some already are;
      audit them all).
    - On Adw.Application's `shutdown` signal, run a final
      `conn.close()` so the DB lock is released cleanly even if a
      thread is still finishing.

## Multi-host / NFS
See `docs/design/database-and-nfs.md` for the full concurrency
model. Single-writer-at-a-time on a shared library is safe; two
active editors on two hosts is not. These items would harden it.
- **Polling watcher fallback over NFS.** `GFileMonitor` is
  inotify-backed, which doesn't fire for sidecar writes from
  another NFS client. Layer a periodic `os.scandir` of the library
  on top of the existing watcher signal and diff mtimes; only trip
  it when the library root is detected as a network filesystem
  (re-use `index.is_network_filesystem`). Catches host-A-edits-while-
  host-B-runs.
- **Hostname-suffixed sidecar tmp paths.** Switch
  `sidecar.write` from `path + ".tmp"` to `path +
  ".<host>.<pid>.tmp"`. Concurrent writers from two hosts won't
  stomp on each other's tmp file mid-flush. Doesn't fix
  last-rename-wins but eliminates the corrupt-tmp race.
- **Read-modify-write with mtime check before rename.** Before
  `rename(tmp, sidecar)`, re-stat the sidecar; if mtime changed
  since the read, abort and re-merge. Catches the common
  cross-host edit race instead of silently dropping one of the
  two edits.

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
