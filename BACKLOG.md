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
- Page thumbnails sidebar.
- **Citation-graph view (Local-Citation-Network style).** Embed-free
  native implementation, since the dependency stack is already
  here: Cairo via `Gtk.DrawingArea` for rendering (same primitives
  the viewer uses for highlight overlays), and OpenAlex for the
  data we already half-fetch.
    - **v0 (~half a day):** seed paper as a centred node,
      references in a backward column (left, dots), citers in a
      forward column (right, dots), nodes positioned by year.
      No within-set edges yet. Click a node → reuse the
      reference-popover machinery from the viewer (it already
      consumes OpenAlex-resolved metadata).
    - **v1 (1–2 days):** within-set edges — the actual *network*
      bit. For each node in the seed-neighbourhood, fetch its
      `referenced_works` and draw an edge whenever the target
      is also in the neighbourhood. Top-N filter on citers
      (popular papers can have thousands of citers — the graph
      is unreadable without a cap). Hover tooltips with
      title / authors / year. Pan and pinch-zoom via
      `Gtk.GestureZoom` on the drawing area.
    - **v2:** persistence — cache the computed network in the
      sidecar so re-opens are instant. Same shape as the
      existing `cited_by_cache` / `references_cache` slots.
    - **Layout choice worth stealing from Local Citation
      Network:** year on x-axis, journal-cluster on y-axis.
      Stable, meaningful, no force-directed graph-physics
      library needed.
    - **Cost on the OpenAlex side:** O(N) calls where N is the
      neighbourhood size (one extra `referenced_works` fetch per
      node, since the seed paper's ref / citer lists carry the
      first-degree edges already). For a typical seed paper with
      ~30 refs and a top-50 citer filter, that's ~80 calls,
      well-cached. No new APIs needed; `metrics.fetch_references`
      and `metrics.fetch_cited_by` already exist.

## Discovery
- **Watch / subscription feed (Wispar-shaped).** A "follow this
  journal / save this OpenAlex search and tell me what's new"
  feature. Designed by reading Wispar's `feed_service.dart` /
  `feed_api.dart` / `home_screen.dart` (cloned at
  `~/Projects/wispar/Wispar`); see also
  `chat-stuff/competitors.md`. Their architecture is sound and
  worth borrowing, with adaptations for our different shape
  (sidecar-as-truth, no cloud sync).

  **Three layers:**
    1. **Subscriptions** — a new SQLite table
       `subscriptions(id PRIMARY KEY, kind, query, name,
       last_fetched, fetch_interval_hours)` where `kind` is
       `'journal_issn' | 'openalex_query' | 'crossref_query'`.
       Wispar's saved-query model is a strict superset of
       "follow a journal" (a journal-follow *is* an OpenAlex /
       CrossRef query for `issn:X sort=created`), so we keep
       just the one table — no separate followed-journals
       table.
    2. **Refresh** — a background pass on the watcher thread
       walks subscriptions whose `last_fetched` is older than
       their interval, runs the stored query, and writes hits
       into a new `discovered(subscription_id, doi,
       openalex_id, title, authors_json, journal, year,
       fetched_at)` table. Separate from the indexed `papers`
       table so unimported discoveries don't pollute the
       library. Batched and concurrency-limited (Wispar caps
       at `maxConcurrentUpdates`); cleans up rows older than
       N days. Manual refresh affordance for impatient cases.
    3. **Custom-feed filters** — saved views over the
       `discovered` rows. Wispar's `FeedFilter` shape:
       `(name, journals: Set<String>, include: String,
       exclude: String, date_mode, date_after, date_before)`.
       Filter is purely client-side over already-cached rows
       (no extra fetch on filter change), tokenised include
       / exclude over `title + abstract + journal`, optional
       date predicate. Multiple filters per user; a feed
       switcher in the header flips between them; a "Home"
       pseudo-feed is the unfiltered union.

  **UI surface:** new tab in the existing Discover dialog —
  "Watch" — listing recent items with per-subscription custom
  filters. Each row has the same "Save to library" / "Get
  PDF" affordances the existing Discover author-search results
  have, so importing follows the established ghost-add path.

  **What we're explicitly NOT taking from Wispar:**
    - PocketBase cross-device sync (we'd use git-sync or
      sidecar-sync, not vendored cloud).
    - Background notifications (Wispar is a journal-alert
      mobile app; we're a desktop library manager).
    - A separate articles table — we'd have `discovered` and
      reuse `papers` for actual library entries. The two
      tables join on DOI / OpenAlex ID for "already in
      library" highlighting.

  Scope estimate: subscriptions table + refresh loop is one
  evening; the custom-filter UI is a second evening; polish
  + dialog wiring is a third.

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
- **"This author cites" / "Cited by" author lists in the author
  dialog.** Extend the existing coauthors section in
  `author_works.py` with two more compact lists:
    - **Cites most:** top-N other authors that this author's
      papers reference. For each of A's works, OpenAlex gives
      `referenced_works`; for each referenced work, fetch
      authorships (batchable via `ids.openalex:W1|W2|…` —
      ~30–50 calls for a typical career). Tally author IDs,
      exclude A (self), keep top 10–15 by count.
    - **Cited by most:** top-N authors whose papers cite A's
      papers. Same iteration as `compute_citing_impact` but
      tally citing-paper authorships instead of citation counts.
      Self-exclusion via the same OpenAlex filter negation.
    - Click an author → open their author dialog (already
      supported by `author_works.py`).
    - **Same cache shape as the citing-impact score:** roll into
      the planned `author_scores` table or a sibling
      `author_relations(openalex_id PRIMARY KEY,
      cites_top_json, cited_by_top_json, computed_at)`. ~30-day
      TTL. Background pass shares the citing-impact loop's
      walk over distinct OpenAlex IDs across library sidecars.
    - **Why this beats the graph for v1:** much higher
      information-density per pixel, no layout problem to
      solve, and the social signal — "Cowtan cites Sheldrick
      and Murshudov; is cited by [young protégés]" — is the
      part of a citation graph that actually carries meaning
      to a researcher.

  Defers the Cairo citation-graph item below: ship this first,
  see whether the graph still feels missing afterwards.
- **Citing-impact score per author.** `metrics.compute_citing_impact`
  is shipped — sums `cited_by_count` across every paper that cites
  any of an author's papers (self-cites excluded server-side via
  OpenAlex's filter negation). Returns `{total, mean, n_citing,
  computed_at}`. Smoke-tested on Cowtan: 65 k citing papers, mean
  49.5 citations per citer, ~4 min runtime. What's still open:
    - **Per-author SQLite cache.** New table
      `author_scores(openalex_id PRIMARY KEY, citing_total,
      citing_mean, n_citing, self_excluded, computed_at)` —
      keyed by OpenAlex ID, ~30-day TTL. Lookup keyed off the
      `authorships` blob in each sidecar.
    - **Background refresh loop.** Same shape as the existing
      citation-refresh loop in `browse.py`: walk distinct
      OpenAlex author IDs across library sidecars, compute
      missing or stale, write rows. Surface `[citing-impact]`
      progress on the status line.
    - **UI chip on the author dialog and (maybe) paper cards.**
      Tooltip should call out the dominating-paper caveat: when
      one paper provides the bulk of the citing pool the metric
      collapses to "average impact of papers that use this
      author's most-cited work" rather than "this author seeded
      thinking that propagated". Consider rendering both `total`
      and `mean` so the user can spot the lopsided case from
      `total / paper_count`.
    - **Erdős-style "prize distance".** Loosely related: shortest
      path from any author to a Nobel/Lasker/Wolf/Turing/Fields
      laureate via coauthorship. See the awards-chip item for
      data plumbing; live BFS via OpenAlex is too costly past
      depth 1, so realistic shapes are (a) cached JIT to depth 2,
      or (b) precompute against the OpenAlex monthly snapshot
      for distance ≤ 2 from laureates.
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

## CrossRef integration
We currently use CrossRef for two thin things — `_crossref_count`
and `_crossref_lookup` for title/authors/year fallback during
extraction. CrossRef returns a lot more on `/works/{doi}` that
we're throwing away. Items below ranked impact ÷ effort. None
need anything beyond the polite pool we already use; identifying
ourselves via `mailto:` in `User-Agent` is in place via
`extract.CROSSREF_USER_AGENT`.

- **Use CrossRef's `reference` array as a third bibliography
  source.** `/works/{doi}` returns a `reference` field when the
  publisher deposited the bibliography (Springer, Wiley, IOP,
  increasingly Elsevier). Each entry is structured: `{ key, DOI,
  author, year, journal-title, volume, first-page, unstructured }`.
  Insert this as a fallback between OpenAlex's `referenced_works`
  (currently primary) and our PDF text parser (currently fallback)
  in `metrics.fetch_references`. Bypasses every PDF parsing
  hazard — column merges, hanging indents, year-paren splitter
  bugs — for the meaningful fraction of papers where the
  publisher deposited references. ~30 LOC. Highest impact.

- **Crossmark / `update-to` chip.** `GET /works/{doi}/update`
  returns updates pointing to this paper. Render a chip on the
  card: "⚠ Correction issued (2023)", "⚠ Retracted",
  "Updated by [link]". Genuinely unique among competitors —
  Zotero, Mendeley, Wispar all skip this. One extra call per
  paper, runs during the existing citation-refresh pass. ~50 LOC
  + chip rendering.

- **Preprint↔published-version via `relation` field.** Replace
  `metrics.find_published_version`'s OpenAlex title-search
  heuristic with CrossRef's authoritative `relation:
  { is-preprint-of, is-version-of, has-version }` field on the
  preprint's own DOI record. Cheaper (no search), more accurate
  (publisher-deposited). ~20 LOC, simplifies an existing path.

- **Funder chip.** `funder: [{ name, award: [...] }]` on
  `/works/{doi}`. Card chip "Funded by NIH R01-…". Quick win for
  biomedical users. ~10 LOC + chip rendering.

- **License chip.** `license: [{ URL, content-version, start }]`.
  Coloured chip — green for CC-BY, amber for paywalled, etc.
  Distinguishes OA vs paywalled at a glance without opening the
  paper. ~10 LOC + chip rendering.

- **Honour `X-Rate-Limit-Limit` / `X-Rate-Limit-Interval`
  headers.** CrossRef returns these on every response and asks
  callers to adapt dynamically rather than hard-coding a rate.
  Today our HTTP helper ignores them. Polish item: read the
  headers, track allowed-requests-per-window per-thread, sleep
  briefly when we'd exceed it. Also wire exponential backoff on
  5xx (currently `return None` on any error, which works but is
  pessimistic). Same handler can be reused for OpenAlex which
  emits similar headers. ~40 LOC in a new `_polite_get_json`
  wrapper around `_http_get_json`.

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
