# pdforg Backlog

Pending features, roughly grouped. Newest at the top of each section.

## Top priority

- **Before v0.2: unguarded-network-call sweep.** Audit every GTK
  click / activate handler in the codebase for synchronous network
  calls on the main thread. Known-clean sites: the citation /
  author-score / feed refresher threads, `_on_find_doi` (uses
  `threading.Thread` + `GLib.idle_add`), the BibTeX-ghost
  Get-PDF flow. Not yet audited end-to-end: every other place a
  button or menu entry kicks off OpenAlex / CrossRef / Unpaywall
  / DOI-resolve work. Practical grep is something like
  `grep -nE "connect\([\"']clicked|connect\([\"']activate" pdforg/*.py`
  and walk the call chain from each callback into the metrics /
  feed / discover layer, flagging any `requests.get` (or
  `_http_get_json`) that's reachable without an intervening
  thread + `GLib.idle_add` for the UI update. Probably 10 minutes
  for the audit, plus whatever fixes fall out. Prompted by a
  Gemini code-review pass that flagged the pattern but didn't
  actually find a violation — would be good to be able to claim
  the property holds globally rather than per-spot-check.

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
    - **Forward-compatibility note (v0.1.0).** Today's on-disk
      decisions don't paint us in: `*.pdf.alexandria` is a
      per-file extension, identical inside a flat library or a
      `<category>/` subdir; the per-host SQLite filename
      `library.<host-hash>.db` extends naturally to
      `$XDG_STATE_HOME/Alexandria/<category>/library.<host-hash>.db`
      — one state dir per category, otherwise identical. The
      backwards-compatible migration: an existing v0.1.0 flat
      library is treated as the implicit `default` category; no
      file moves needed on upgrade. Categories are an additive
      change against the v0.1.0 format.
    - **Shared folders (read: someone else's directory).** Not
      really an Alexandria feature — it's a permissions topic.
      Outside Flatpak it's pure Unix: the user mounts /
      symlinks / chmods their shared library somewhere and
      Alexandria reads it. Inside Flatpak the sandbox only
      sees `xdg-documents` by default; advanced users grant
      additional paths via Flatseal or `flatpak override --user
      --filesystem=/path/to/share`. Document the Flatseal path
      in README under "advanced setup" rather than build a
      bespoke "Shared Folders" UI. Cross-host concurrency over
      a shared library is still bound by the
      single-writer-at-a-time guarantee documented in
      `docs/design/database-and-nfs.md` — two simultaneous
      editors on two hosts is the unsolved race.
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
- **Unpaywall as a `Get PDF` fallback.** Currently "Get PDF" on a
  ghost card chases OpenAlex's `open_access.oa_url`, which is fine
  when it resolves but misses a long tail. Unpaywall (same parent
  org as OpenAlex; the OpenAlex blog flagged a planned alignment
  pass in summer 2025 but the two are not yet fully synced) is
  the upstream canonical OA-lookup service that Web of Science /
  Scopus / Dimensions / Wispar all consume. One endpoint:
  `https://api.unpaywall.org/v2/{doi}?email=…`, email required,
  100 k requests/day, no API key. Returns `is_oa`, `oa_status`
  (`gold`/`hybrid`/`green`/`bronze`/`closed`), `best_oa_location`,
  and an `oa_locations` array. Each location has `url_for_pdf`,
  `host_type` (`publisher` vs `repository`), `version`
  (`publishedVersion`/`acceptedVersion`/`submittedVersion`),
  `license`, `repository_institution`. Three things we can do
  with it:
    - **`Get PDF` fallback.** When OpenAlex's `oa_url` is missing
      or 404s, ask Unpaywall. Wraps a single helper in
      `metrics.py` (`fetch_oa_locations(doi)`) mirroring the
      shape of `fetch_cited_by` / `fetch_references`. Sits
      between OpenAlex (try first) and EZproxy (try last) in
      the `Get PDF` ladder.
    - **Version-aware preference.** OpenAlex hands us one URL;
      Unpaywall's array lets us prefer `publishedVersion @
      publisher` over `acceptedVersion @ repository` over
      `submittedVersion @ preprint`. Useful when the user cares
      which copy lands on disk — the published copy reads
      identically to the paywalled one, the accepted manuscript
      doesn't.
    - **OA-status chip on cards.** Persist `oa_status` in the
      sidecar (next to existing OpenAlex fields) and render a
      small badge (`OA`, `green`, `hybrid`, etc.) on the card.
      Useful at-a-glance signal — "is there a free copy of
      this paper anywhere" — without round-tripping every open.
  Diminishing-returns caveat: once OpenAlex's planned Unpaywall
  alignment lands, the `Get PDF` fallback win will shrink to the
  tail. The version-aware sort and the OA-status chip are the
  durable differentiators. (See `chat-stuff/competitors.md`.)

  Not redundant with the Save-to-Alexandria Firefox extension
  (sister project at `~/Projects/alexandria-firefox-extension`).
  The extension only fires when the user is browsing a publisher
  page in Firefox — it's the *collection-side* path. The
  API-based fallback documented here fires from inside the app
  on ghost cards (BibTeX imports, DOI-paste imports) where
  there's no browser session. Both should exist. The extension
  also composes naturally with the Unpaywall *browser*
  extension (read-side, finds free PDFs) — see
  `docs/related-unpaywall-extension.md` in the sister repo.

- **EZproxy support for paywalled fetches.** Currently "Get PDF"
  on a ghost card chases OpenAlex's `oa_url` only — fine for
  open-access papers, useless for paywalled ones. EZproxy is
  the standard URL-rewriting proxy university libraries run for
  off-campus subscription access. Adding support is small:
    - New Preferences entry "EZproxy URL", e.g.
      `https://login.ezproxy.harvard.edu/login?url=`.
    - When `oa_url` is missing or returns paywall HTML, prefix
      the publisher URL with the user's EZproxy URL and open it
      in their default browser. The user already has a session
      with their institution; we don't handle auth.
    - Optional: surface a "Open via EZproxy" affordance on
      every card with a DOI, regardless of OA status, for users
      who'd rather always go through their institution.
  Implication for `Get PDF`: for institutionally-affiliated
  users, it goes from "OA-only" to "anything our institution
  subscribes to". Wispar already has this; competitive parity
  point as well as a real workflow improvement. (See
  `chat-stuff/competitors.md`.)

- **"Fetch when inside the firewall" waiting list.** When the
  user is off-campus they often find a paper they *can't* get now
  but *could* get through their institution's subscription once
  back inside the firewall (or on VPN/EZproxy). Today the only
  option is "remember to do it later", which doesn't scale.
  Sketch:
    - A per-paper "Add to fetch-later list" action on any card /
      ghost / discovered row where the PDF couldn't be retrieved.
      Stores `{doi, title, added_at, source_url}` in a small
      `fetch_queue` table (sibling of `subscriptions`).
    - A "Fetch list" view (hamburger), like the Subscriptions
      window: shows queued items, manual "Try now" per row, and
      a bulk "Try all".
    - **Auto-trigger when the network changes.** The hard part is
      knowing we're "inside" again. Options, cheapest first:
      (a) manual "I'm on the network now → try all" button (v1);
      (b) detect a successful reach of the EZproxy host / a
      user-configured "inside" probe URL and surface a toast
      "You can now fetch N waiting papers";
      (c) watch for default-route / DNS-suffix change (Linux:
      poll `/proc/net/route` or netlink) as the trigger for (b).
    - Composes with the EZproxy item above (that's the fetch
      mechanism) and the Subscriptions refresher pattern (that's
      the background-loop shape). v1 is just the table + the
      manual "Try all" button; the network-aware auto-trigger is
      the nice-to-have follow-up.

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
  feature, designed by reading Wispar's `feed_service.dart` /
  `feed_api.dart` / `home_screen.dart` (cloned at
  `~/Projects/wispar/Wispar`).

  **v1 shipped:**
    - **Subscriptions** table — `subscriptions(id, kind, name,
      query, fetch_interval_hours, last_fetched, created_at)`
      with kind ∈ `journal_issn | openalex_query |
      crossref_query` (last reserved, not wired). One table
      covers both follow-a-journal and save-a-search.
    - **Discovered** table — `discovered(subscription_id, doi,
      openalex_id, title, authors_json, journal, year,
      published_date, abstract, is_oa, oa_url, fetched_at)`,
      `UNIQUE(subscription_id, doi)`, ON DELETE CASCADE so
      removing a sub cleans up. Separate from `papers` so
      unimported hits don't pollute the library.
    - **Refresh** — third daemon thread in BrowserWindow,
      `_feed_refresher`. Wakes every 15 min, walks
      `stale_subscriptions`, refreshes those whose own
      `fetch_interval_hours` (or the 6 h default) has expired.
      Prunes `discovered` rows older than 60 days every fourth
      pass.
    - **Subscriptions window** (`pdforg/feed_window.py`) —
      opened from the hamburger menu. Top strip of pills
      (current follows, click to filter the body, × to
      remove). "Add…" popover with Journal / Topic toggle;
      journal mode runs `find_journal_by_name` (rows=100 +
      exact-title re-rank, or ISSN-direct when input matches
      `\d{4}-?\d{3}[\dxX]`). Body is a card list of
      discovered articles with title / journal / date /
      authors / abstract + a "Get PDF" button that routes
      through `BrowserWindow.add_reference_from_viewer` so
      the existing ghost-import path is reused.

  **Still open:**
    - **Custom-feed filters.** Wispar's `FeedFilter` shape:
      `(name, journals: Set<String>, include: String,
      exclude: String, date_mode, date_after, date_before)`.
      Filter purely client-side over `discovered` rows
      (no extra fetch on filter change), tokenised
      include/exclude over `title + abstract + journal`.
      Multiple filters per user; a feed switcher in the
      header.
    - **"Already in library" badge.** When a discovered
      article's DOI matches a row in `papers`, render an
      in-library glyph instead of (or alongside) the
      Get-PDF button. Reuses
      `BrowserWindow.find_existing_by_doi` already used by
      the reference popover.
    - **Per-subscription cadence override UI.** The schema
      column exists; no UI yet to set it. Power-user need
      (Science weekly, bioRxiv hourly).
    - **Live-update notification.** The refresher already
      calls `_on_feed_updated`; the open feed window
      auto-refreshes. Could surface a transient toast
      "N new articles in <subscription>" on the main
      browser too.
    - **`crossref_query` kind.** Reserved in the schema; UI
      and fetcher branch not wired. Wispar's saved-query
      model supports CrossRef arbitrary filter strings too.

  **What we're explicitly NOT taking from Wispar:**
    - PocketBase cross-device sync (use git-sync or
      sidecar-sync if anything).
    - Background OS-level notifications (Wispar is a
      mobile alert app; we're a desktop library manager).

- **Library-aware feed re-ranking (ASReview-inspired).** Today
  the subscription feed shows new articles in publication order.
  As the number of follows grows it becomes a low-signal
  firehose; the user wants "papers most like what's already in
  my library" first. Treat the library as implicit positives,
  dismissed/ignored feed entries (after N days unread) as
  implicit negatives, train a tiny scorer over `title +
  abstract + auto_keywords + journal`, re-rank the feed by
  that score.
    - **v0 — TF-IDF cosine.** Sparse vector per paper from
      title + abstract + keywords + journal. Score each feed
      candidate by max-cosine-similarity against the library
      (or top-k mean). No training, no model file. One
      dependency: `scikit-learn`'s `TfidfVectorizer`, or
      hand-rolled to stay light. Update incrementally as new
      library rows arrive.
    - **v1 — Logistic regression with explicit negatives.**
      Once "dismiss" is a UI affordance on the feed, add
      logistic regression on the TF-IDF vectors: library as
      positive class, dismissed-or-aged-out as negative.
      Refresh nightly. Show a ★/★★/★★★ relevance chip on
      each card so the user can see why something ranked
      high (top similar library paper? top topic overlap?).
    - **What's NOT in scope.** Full active-learning loop
      (label-one, retrain, label-next) — overkill at ~20 new
      feed items/day, pointless cold-start cost. We don't have
      ASReview's volume or its exhaustive-recall goal.
    - **Why ASReview as inspiration.** Their published results
      (Van de Schoot et al., *Nature Machine Intelligence* 2021)
      show simple TF-IDF + linear classifier beats transformers
      for systematic-review screening — small, sparse,
      interpretable wins in the same regime we're in. See the
      ASReview entry in `chat-stuff/competitors.md` for the
      shape comparison.

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

- **PI funding profile in the author dialog (multi-source).**
  Aggregate an author's grants across their works and enrich them
  from public funder databases so the dialog shows the
  funding↔paper relationship — a likely "selling feature" for the
  author view. Today OpenAlex `grants` (`funder` + `award_id`) are
  already surfaced *per work* as a Funded-by chip
  (`author_works.py:1067`); this item is the *aggregated, per-PI*
  view plus external enrichment.
    - **Sources, roughly best-first by API quality:**
      - OpenAlex `grants` — already in hand; aggregate across works.
      - UKRI Gateway to Research (GtR) — clean REST API, explicit
        PI/CoI roles, project value + dates. Best first external
        target.
      - NIH RePORTER (v2 JSON API) — PI names, project numbers,
        funding amounts, linked publications.
      - NSF Award Search (api.nsf.gov) — award abstracts, PI, amounts.
      - Federal RePORTER / OSTI.GOV — broader US federal coverage;
        lower priority, more heterogeneous.
    - **Hard part is entity resolution, not fetching.** These gov
      funders mostly key on **grant number + PI name + institution**,
      not ORCID, and ORCID coverage is patchy. Design the
      PI-matching layer first (ORCID where present; name +
      institution + funder-grant cross-checks otherwise) and treat
      each funder as a pluggable adapter behind a common interface,
      shipping one source at a time starting with GtR.
    - **Caching: do NOT widen `author_works_cache` /
      `author_scores`.** Those are clean single-purpose, openalex_id-
      keyed caches. Add a separate source-tagged cache, e.g.
      `author_funding_cache(author_key, source, payload_json,
      fetched_at)` with per-source TTLs, so each provider fetches,
      expires, and *fails* independently (gov endpoints are flaky;
      independent degradation matters). Store a thin normalised grant
      model `{funder, award_id, title, pi, amount, currency, start,
      end, source, source_url}` for display, alongside the raw
      payload for re-derivation.
    - Worth a full brainstorm → spec before implementation; this
      spans several subsystems (matching, adapters, cache, UI).

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
  OpenAlex's filter negation). Now bucketed by paper kind via
  `classify_paper(title)`: returns `{software, method, idea}` each
  with `{total, mean, n_citing, n_works}` plus `computed_at`. The
  buckets answer three different questions ("did people cite this
  author as software? as method? as idea?") so a citing paper can
  land in multiple buckets — dedup is per-bucket, not global.
  Smoke-tested on Cowtan: software 19 works / 41 k citing /
  mean 50.9; method 9 works / 337 citing / mean 116; idea
  93 works / 33 k citing / mean 48.1; ~4 min runtime. The method
  bucket having the highest mean is the kind of signal this metric
  exists to surface — Cowtan's algorithm papers (DM, PIRATE, etc.)
  get cited by deeper-impact downstream papers than either his
  software or his findings papers. What's still open:
    - **Per-author SQLite cache.** New table
      `author_scores(openalex_id PRIMARY KEY,
      software_total, software_mean, software_n_citing, software_n_works,
      method_total, method_mean, method_n_citing, method_n_works,
      idea_total, idea_mean, idea_n_citing, idea_n_works,
      self_excluded, computed_at)` —
      keyed by OpenAlex ID, ~30-day TTL. Lookup keyed off the
      `authorships` blob in each sidecar.
    - **Background refresh loop.** Same shape as the existing
      citation-refresh loop in `browse.py`: walk distinct
      OpenAlex author IDs across library sidecars, compute
      missing or stale, write rows. Surface `[citing-impact]`
      progress on the status line.
    - **UI chip on the author dialog and (maybe) paper cards.**
      Three numbers, not one — render the bucket totals
      side-by-side so the dominating-paper caveat (one Coot
      paper provides most of the software bucket) is visible
      from the shape of the data, not buried in a tooltip.
      Consider rendering both `total` and `mean` per bucket so
      the user can spot the lopsided case from `total / n_works`.
    - **Per-paper kind override.** The title heuristic in
      `classify_paper` will misfire on review papers ("a review
      of methods…" → method) and on mis-titled software. Add a
      sidecar field `paper_kind: software|method|idea` that
      wins over the heuristic when set, surfaced as a small
      dropdown in the metadata editor.
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

## Sharing

The on-disk format renamed sidecars to `*.pdf.alexandria` in
v0.1.0 with this whole feature in mind: a recipient who gets a
`.alexandria` file via email / Slack should be able to open it
into their own library with zero friction. Three follow-ups
deferred from the design discussion (v0.1.0 ships with the
filename change but none of the integration yet):

- **Drag-and-drop sidecar (or sidecar + PDF) out of the main
  interface.** Each card currently has Open / View / Edit /
  Rename / Delete actions. Add a draggable source for the
  sidecar — drag the card itself (or a small "share" handle)
  and the drag payload is the `*.pdf.alexandria` file. If the
  user grabs the PDF thumbnail/icon area, the drag payload is
  the PDF instead. Existing GTK4 `Gtk.DragSource` machinery;
  payload mime types `application/vnd.alexandria-share+json`
  for the sidecar, `application/pdf` for the PDF.

  **Why both as separate handles, not a single "share"
  action:** the on-disk convention is that PDFs are referenced
  by *basename* from inside the sidecar (`pdf_filename` is a
  relative name), and the read path assumes PDF + sidecar live
  in the same directory. So a recipient who drops *both* files
  into their library directory gets a working pair with no
  path rewriting. Honour that property in the export path: do
  NOT rewrite `pdf_filename` to an absolute path on drag.

  **For the v1 sender-side strip:** before handing the
  `.alexandria` payload to the drag source, strip any
  sender-absolute paths from the sidecar copy (`pdf_path`,
  `sidecar_path`, `thumb_path`) and add a provenance
  breadcrumb (`shared_from: {sender, exported_at}`). The
  on-disk file in the library is unchanged; the dragged copy
  is the cleaned export.

- **MIME registration so `.alexandria` opens Alexandria.** Two
  small additions:
    - `MimeType=application/vnd.alexandria-share+json;` in the
      existing `data/io.github.pemsley.Alexandria.desktop`.
    - A new `data/io.github.pemsley.Alexandria.mime.xml`
      declaring the type and binding `*.alexandria` to it,
      with `<sub-class-of type="application/json"/>` so
      JSON-aware tools still recognise the format. Install in
      the Flatpak manifest under
      `/app/share/mime/packages/`.

  After this, double-clicking a `.alexandria` file in Files /
  Nautilus opens Alexandria with an import preview dialog
  (see next item). On macOS the equivalent is `Info.plist`
  `CFBundleDocumentTypes` + `UTExportedTypeDeclarations` —
  needs an `.app` bundle (Platypus would do the wrapping; see
  the memory entry). Not relevant for the Flathub ship.

- **Import preview dialog.** When a `.alexandria` (or
  `.alexandria.zip` bundle) is dragged onto the browser window
  or opened via MIME handler, surface a dialog instead of
  silently importing:
    - Title / authors / abstract preview, sender name from
      the `shared_from` provenance.
    - Checkboxes for what to merge: `[✓ metadata] [✓ tags]
      [✓ notes] [☐ highlights] [☐ comments]` with sane
      defaults (metadata + tags + notes by default; sender
      annotations off — too easy to assume they're yours).
    - Collision UX: if the recipient already has the paper
      (DOI / sha256 match), show a "Merge into existing row"
      panel with conflict markers in the comments rather than
      silently overwriting.
    - Auto-fetch when sidecar-only: if the bundle is metadata-
      only and the recipient doesn't have the PDF, offer to
      try Unpaywall / OA path. Re-resolve through *our*
      Unpaywall call — never trust the sender's `oa_url`
      verbatim (security: don't follow a sender-controlled
      URL).
    - Hash verify when bundle includes the PDF: if the
      bundle's sha256 doesn't match the sidecar's, refuse and
      explain. Catches "wrong file attached" mistakes
      honestly.
    - Buttons: `[Add to library] / [Cancel]`.

## Editor

The sidecars contain many more fields than are editable.
We want to be able to edit/see the fields that would be
exported to bibtex.

## Import Failures

  - $HOME/Documents/Alexandria/acs.jcim.4c02293.pdf
    Find references in body text, but cannot extract the reference.

## CrossRef integration
We currently use CrossRef for: `_crossref_lookup` (title/authors/year
fallback during extraction), the citation count + authorships fallback
in `fetch_metrics` (`_crossref_authorships`, used when OpenAlex has no
record for a fresh DOI), and `_fetch_crossref_work_message` /
`fetch_crossref_extras` (license, crossmark). CrossRef returns more on
`/works/{doi}` that we're still throwing away. Items below ranked
impact ÷ effort. None need anything beyond the polite pool we already
use; identifying ourselves via `mailto:` in `User-Agent` is in place
via `extract.CROSSREF_USER_AGENT`.

- **Crossmark / Retraction Watch — bulk-CSV option.** The
  per-DOI `updated-by` path is shipped (`_crossmark_from_message`
  in `metrics.py`, severity-ranked, rendered by
  `make_crossmark_chip` on the card). Still open: for whole-
  library or offline scans, mirror the full Retraction Watch
  daily-updated CSV at
  `https://api.labs.crossref.org/data/retractionwatch` and join
  by DOI instead of per-paper calls. Only worth doing if we ever
  need to flag retractions without touching the network.

- **CrossRef funder fallback.** OpenAlex `awards`/`funders` is
  the primary source for the card "Funded by" line and the
  author-dialog row, but coverage is patchy — many works have
  zero entries. CrossRef's `/works/{doi}` carries a
  `funder: [{ name, DOI, award: [...] }]` block we currently
  drop. Pull it in `_fetch_crossref_work_message` as a fallback
  when the OpenAlex grants list is empty; map into the same
  `[{funder, award_id}]` shape we already persist. Composes with
  the aggregated PI funding profile item under Discovery.

## OpenAlex client

- **Migrate to PyAlex for all OpenAlex HTTP.** Today `metrics.py`
  has 17 OpenAlex callsites, each driving `_http_get_json`
  through a hand-rolled retry/throttle loop. PyAlex
  (`docs.openalex.org` reference client, see entry in
  `chat-stuff/competitors.md`) does the same job better in the
  parts that matter:
    - **Proactive throttling** based on `X-RateLimit-Remaining`
      rather than only reacting to a 429 after the fact. Our
      circuit breaker stops the bleeding; PyAlex stops the
      cause.
    - **Cursor pagination as a generator** instead of the manual
      while-cursor loop we repeat in every helper.
    - **Filter/sort DSL** — `Works().filter(...).sort(...)`
      replaces `urlencode([("filter", ...), ("sort", ...)])`.
    - **Absorbs upstream API churn** (e.g. the credits-USD
      pricing introduced this week) as part of its maintenance
      cycle. We won't notice when it happens.

  **Scope: full migration**, not a half-step. Doing only the
  heaviest helper (`compute_citing_impact`) leaves us with two
  patterns to maintain forever; the win comes from one consistent
  shape across `metrics.py`.

  **What changes:**
    - Add `pyalex` to `requirements-flatpak.txt` /
      `python-deps.yaml`. Pure-Python on PyPI, no transitive
      pain expected.
    - Rewrite `metrics.fetch_*` helpers around PyAlex's
      typed objects. Public signatures of each helper stay the
      same so call sites in `browse.py` / `feed_window.py` /
      `author_works.py` / `viewer.py` don't change.
    - Adapt our session circuit breaker into PyAlex's retry
      hook (or wrap PyAlex calls in a thin guard that checks
      `openalex_paused_until()` first).
    - Keep CrossRef and Unpaywall paths on the existing
      `_http_get_json` — they're roughly half our HTTP work and
      PyAlex doesn't help there.

  **Why not yet:** real refactor surface across ~15 helpers; want
  the daily quota back so each migrated helper can be verified
  live before the next is touched. Bring up after midnight UTC
  some evening when there's a clear session to spend.

- **SemOpenAlex as a side-channel for ontology + federated
  queries.** Not a rate-limit workaround — see the writeup in
  `chat-stuff/competitors.md`. SemOpenAlex (Färber et al.,
  KIT/metaphacts; ISWC 2023 best paper) is a public no-auth
  SPARQL endpoint at `semopenalex.org/sparql` projecting the
  OpenAlex AWS snapshot into RDF (~26B triples, CC0). Monthly
  stale, undocumented per-query timeouts on a shared GraphDB
  instance — so it can't replace our live REST traffic for
  cite counts / find-by-DOI / find-published-version. What it
  *uniquely* offers:
    - **`owl:sameAs` to ORCID, ROR, Wikidata, DBpedia** as
      first-class triples. Federated SPARQL via `SERVICE
      <https://query.wikidata.org/sparql>` joins across
      knowledge graphs in one round-trip. A "Wikidata-linked"
      chip on the author dialog drops out of one query rather
      than an OpenAlex call + a Wikidata call + a manual join.
    - **SKOS concept hierarchy traversal** via `skos:broader+`
      in a single query — enables a parent/child topic browser
      for the subscription feed's topic-of-interest affordance.
    - **Cross-source identifier resolution** — given a DOI,
      return ORCID + ROR + Wikidata + DBpedia IDs in one shot
      for use by future "Look up author in Wikipedia",
      "Validate institution against ROR", etc.

  **What it'd take.** New module `pdforg/semopenalex.py` wrapping
  `SPARQLWrapper` (PyPI, light dependency) or hand-rolled
  HTTP-to-`application/sparql-results+json`. Each feature ships
  as its own helper with explicit fallback when the endpoint is
  slow or down — SPARQL endpoints fail differently from REST
  APIs and the caller has to be ready.

  **Use only for the features REST can't cover.** Specifically
  *not* a substitute for any current `metrics.py` helper. Keep
  the existing OpenAlex REST path for everything live; use
  SemOpenAlex only when crossing into Wikidata / ORCID / ROR
  enrichment or doing ontology traversal.

  Caveats flagged in the survey: snapshot freshness only
  documented up to 2023-03-28 in the paper; the "monthly
  refresh" cadence is a pipeline goal, not a measured fact; no
  public SPARQL-vs-REST latency benchmarks for this endpoint.
  Verify any of these before building against them.

## Sorting & filtering
- Tag chips + filter sidebar
- FTS to include mark labels

## Export
- BibTeX export (single, filtered, whole library).
- **User-supplied CSL styles.** "Cite this paper as…" currently
  ships APA / Vancouver / Nature / Chicago author-date in
  `pdforg/styles/`. Add a Preferences entry that lets users drop
  additional `.csl` files into `~/.config/Alexandria/styles/` and
  have them picked up by `csl_format.list_styles()`.
## Static HTML index (Drive-syncable companion view)

- **Goal: armchair reading on a tablet, via Google Drive sync of
  the library folder.** The user already has Google Drive (or
  Dropbox / iCloud / rclone) syncing `~/Documents/Alexandria/`;
  if Alexandria periodically writes an `index.html` into that
  folder, any tablet with a browser becomes a read-only library
  browser. No Kotlin / Android codebase to build and maintain.
  Decided after a competitor analysis pass where a Gemini review
  suggested building a Kotlin companion app — see
  `chat-stuff/competitors.md` (or the conversation log) for why
  a 1–2-month-minimum Android product was rejected in favour of
  this half-day static-HTML path.

- **File layout.** Single `index.html` at the library root
  (`~/Documents/Alexandria/index.html`). All assets inlined
  (CSS + tiny client-side JS for search/filter) so the file is
  one upload from Drive's perspective. PDF links are *relative*
  basenames — they resolve correctly inside any Drive-synced
  folder regardless of mount point. Aligns with the existing
  property that sidecars reference PDFs by basename only.

- **Contents.** One card per paper, mirroring the desktop
  browser's card layout in a mobile-friendly form: title (linked
  to the PDF), authors, year, journal, citation-count chip,
  mark dot if set, tags. Client-side search box (full-text over
  title + authors + journal + tags) and a filter chip strip
  (mark colour, tag). No abstract by default to keep the page
  light; expand-on-tap for the abstract. Viewport meta + touch
  hit-targets for tablet ergonomics.

- **Source of data.** Walk the library's `*.alexandria`
  sidecars, build a JSON blob, embed it inside a `<script
  type="application/json" id="papers-data">` tag, render the
  page via the embedded JS on load. Reuses the existing sidecar
  format as the single source of truth — no new export schema.

- **Regeneration trigger ladder.**
    - **v1**: a "Generate Drive index" menu entry in the
      hamburger menu. Manual, predictable, zero new background
      work. Good enough for the first useful version.
    - **v1.1**: debounced auto-regen — when a sidecar write
      lands, schedule a regen 30 s later (resetting the timer on
      each new write so a 50-PDF import collapses into one
      regen). Coalesces noise, keeps the file fresh without
      thrashing Drive sync.
    - **NOT v1**: regen on every sidecar write (would thrash
      Drive sync and burn battery), or a separate daemon (too
      heavy for the value).

- **What's explicitly NOT in scope.**
    - Cloud sync built into Alexandria. The user's Drive
      desktop client / rclone / Dropbox client handles file
      transport. We just write the HTML.
    - Bidirectional sync (tablet edits flowing back). Read-only
      by design; sidesteps conflict resolution entirely.
    - Per-paper PDF streaming. The Drive client already lazy-
      downloads PDFs on tap on most platforms.
    - Auth / accounts. The user opens `index.html` from their
      Drive folder; the existing OS-level Drive auth covers
      file access.

- **Why this beats a Kotlin app.** Build time ½ day vs 1–2
  months. Maintenance cost ~zero (HTML + small JS) vs ongoing
  Android API churn. Reach: anything with a browser, not
  Android-only. Trade-off: no offline mode unless the Drive
  client makes the HTML and the PDFs available offline (which
  modern Drive clients do via "make available offline" per
  folder).

## UI

- **Click-through on award IDs in the card "Funded by" line.** The
  award_id is shown today only in the tooltip; it would be far
  more useful if clicking it opened (somehow) the funded
  application's abstract or summary. Gateway to Research
  (`gtr.ukri.org` — the UKRI public-facing grant database) is the
  most likely source for UK funders (UKRI/EPSRC/BBSRC/MRC etc.):
  it exposes a REST API that takes a grant reference and returns
  the project record. NIH RePORTER is the obvious analogue for US
  funders. Per-funder routing: dispatch on the `funder` name to
  pick the right registry; fall through to a Google search for
  the bare award_id when none matches.

## Watcher
- Recursive subdir watching (currently flat on `LIBRARY_ROOT`)
- **Clean process shutdown on window close.** Largely done; one
  refinement open. Original symptom: closing the browser window
  often left the Python process alive in the background
  (`ps aux | grep pdforg-browse` showed zombies from earlier
  sessions). They kept file descriptors open on
  `~/.local/state/Alexandria/library.db`, which meant the next
  launch of Alexandria failed to acquire a WAL lock and aborted
  with "disk I/O error". What's now in place:
    - **All threads audited to `daemon=True`.** browse,
      feed_window, viewer, author_works, discover, watcher.
    - **`Adw.Application.shutdown`** is wired to
      `PRAGMA wal_checkpoint(TRUNCATE)` + `conn.close()` so the
      WAL gets flipped cleanly even if a thread was mid-write
      when killed. Fires after the last window closes and
      before the interpreter exits.
    - **`_on_close_request`** sets stop events on all three
      refreshers (`_cit_stop`, `_asc_stop`, `_feed_stop`) and
      calls `library_watcher.stop()`.

  **Still open:** explicit thread join-with-timeout in
  `_on_close_request`. Daemon threads are killed at interpreter
  exit; a brief 50–100 ms join would give them a chance to finish
  any in-progress sidecar write before the conn closes. Would
  need to store thread references on `self.` — refactor cost only
  worth paying if the zombie symptom recurs.

## Multi-host / NFS
See `docs/design/database-and-nfs.md` for the full concurrency
model. Single-writer-at-a-time on a shared library is safe; two
active editors on two hosts is not. These items would harden it.
- **Sidecar mtime-prefilter for slow / high-latency filesystems.**
  Today `importer.import_pdf`'s existing-branch does ~6 stats per
  PDF (sidecar + thumb recent-guard, sidecar isfile, sidecar
  getmtime, thumb-exists check inside `make_thumbnail`). On local
  SSD that's free (~0.5 ms/PDF, 1800 PDF/s measured). On
  NFS-over-bad-wifi each stat is a fresh round-trip at the
  client's RTT — 100 PDFs × 6 × 30 ms ≈ 18 s before a byte of
  JSON is read. Fix:
    - One `os.scandir(library_root)` per directory: a single
      round-trip returns every entry's mtime.
    - For each `*.alexandria` entry, compare against
      `papers.sidecar_mtime` already in the DB.
    - Only `sidecar.read` + `index.upsert` for the rows whose
      mtime moved (or that aren't in the DB at all).
  Converts "N × 6 round-trips" into "1 scandir + 2 × N_changed
  round-trips" — a quiescent library is near-free regardless of
  latency. New entry-point `index.sync_from_sidecars(conn, root)`
  to call at GUI startup, replacing the implicit walk via
  `import_tree`. Composes naturally with the polling-watcher
  fallback below — same primitive, different trigger.
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

## Not soon, but at some stage

### Publisher-page metadata harvest (extends the citation_pdf_url fallback)

The feed refresher already does a narrow landing-page scrape:
when Unpaywall says `is_oa=True` but has no PDF URL, we hit
`https://doi.org/{doi}`, follow the redirect, and pull
`<meta name="citation_pdf_url">` from the resulting HTML
(`feed._fetch_landing_pdf_url`). That covers the freshness
gap on very-new OA papers without any per-publisher knowledge.

Going further has real wins but per-publisher maintenance cost:

- **Publisher-asserted OA status.** Each publisher embeds its
  own JSON or microdata. Nature's landing pages have a
  `window.dataLayer = [{ ..., "copyright": {"open": true},
  "publishingModel": "Hybrid Access" }]` blob — direct from
  the publisher, no Unpaywall inference. Other publishers
  embed similar but use different keys (Elsevier JSON-LD,
  Wiley Schema.org tags, OUP per-page CSS class, …). Pulling
  this would let us mark articles OA without Unpaywall in the
  loop, useful for the small set of papers Unpaywall hasn't
  indexed.
- **Full bibliographic harvest from `citation_*` meta tags.**
  Google-Scholar requires publishers to expose `citation_title`,
  `citation_author`, `citation_doi`, `citation_journal_title`,
  `citation_publication_date`, `citation_issn`,
  `citation_firstpage`, `citation_lastpage`. We already pull
  most of these from CrossRef/OpenAlex; landing pages would
  give us a third source for cross-checking and for filling
  gaps when the API sources are sparse.
- **License extraction.** `<link rel="license">` or
  `<meta name="prism.copyright">` carries the CC-BY-* etc.
  Feed into the License-chip BACKLOG item.

Cost analysis already done — see the conversation around the
narrow fallback. Roughly:
  - ~1 day for the standard meta-tags fallback (PDF URL only) —
    **already shipped, narrow scope**.
  - + 2–3 days for a per-publisher OA-status extractor for the
    top 5–10 publishers (Springer-Nature, Wiley, Elsevier, OUP,
    Cell Press, ACS, IEEE, Sage, RSC, Cambridge).
  - + plumbing: polite per-host throttling queue, robots.txt
    honouring, Cloudflare-aware UA, ~1 fetch/host/sec floor.

Why "not soon": Unpaywall's OA flag is usually right; what we
needed urgently — a PDF URL when Unpaywall has none — is now
covered. The wider scope is whack-a-mole maintenance with
diminishing returns, and only buys speed-of-recognition for
fresh papers.

### Server Sync

  - Create a server that syncs the library. The client will
    need to tell the server what files it has, each hashed.
  - Make a web server on the sync server, so that we can see what
    has been synced. We will need authentication.
  - Then, start with a simple viewer of the synced metadata and images
    And the ability to search the metadata.

### Communication with Claude

  - How do we get Claude in the loop between our client and
    OpenAlex? Look at projects that use OpenAlex and provide an
    MCP gateway. Verified examples in
    `chat-stuff/competitors.md` (long-tail section):
    `openalex-research-mcp` and `Scientific-Papers-MCP`.

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
