# Alexandria Backlog

Pending features, roughly grouped. Newest at the top of each section.

## Top priority

  Nothing outstanding.

  - *(Closed 2026-08-31: the PDB indexer's "another row available"
    failure. Root-caused and fixed in `ab347ba` —
    `importer._schedule_pdb_indexing` closed over the caller's
    `sqlite3.Connection`, which `watcher._do_import` closes the
    instant `import_pdf` returns, so two threads shared one
    connection; the worker now opens its own via
    `index.connect_existing()`. The item stayed open pending a
    cold-import storm to confirm it under load. That storm has now
    happened: a 185-PDF import through the GUI, 172 papers, zero
    errors, and no such message in the terminal.)*

## pdb viewer zoom

 - Command + to zoom in (on Mac)
 - zoom to fit by default (explore)

## Import / ingestion
- **Construct publisher PDF URLs from the DOI, before asking anyone.**
  Borrowed from bibtui (see
  `chat-stuff/possible-collaboration-bibtui.md`), which derives a
  Copernicus PDF location arithmetically — no API, no key, no lookup:

    ```
    10.5194/tc-17-1585-2023
      → https://tc.copernicus.org/articles/17/1585/2023/tc-17-1585-2023.pdf
    ```

  (verified 2026-09-03: HTTP 200, application/pdf, 1.1 MB), with
  separate shapes for preprints and EGUsphere.

  **Measured value, and it is not what it first looks like.** Our
  existing chain already finds *something* for that DOI — but what it
  returns is `hal.science/hal-04672801v1/document`, a **repository
  copy**, not the publisher's version of record. Construction gets
  the published PDF, and gets it with zero API calls. So the win is
  **quality of copy and independence from OpenAlex/Unpaywall**, not
  coverage. That reframing matters: measured against "does the chain
  find a PDF", this scores nothing.

  This is the exact mirror of the "derive DOIs from publisher
  filename conventions" entry above — same insight, opposite
  direction: **publisher identifiers are structured, not opaque.**
  Worth building both directions against one small table of publisher
  rules. Candidates beyond Copernicus: any publisher whose DOI suffix
  encodes volume/page/year.

  See also the note's suggestion that this rule table is the natural
  thing to **factor out and co-own with bibtui** rather than each
  project maintaining its own.

- **arXiv as a direct fetch strategy.** Also from bibtui, whose chain
  starts arXiv → Copernicus → OpenAlex → Unpaywall → direct-URL.

  Again, be honest about the size of the win: for a DOI-bearing
  preprint our chain **already** resolves
  `10.48550/arXiv.2006.11239` to `https://arxiv.org/pdf/2006.11239`
  (verified 2026-09-03: HTTP 200, 10.3 MB). So this is not new
  coverage. It is worth doing for three narrower reasons:

    - **It works when OpenAlex does not.** We have a circuit breaker
      and credit accounting precisely because OpenAlex fails or runs
      out; a direct arXiv fetch needs neither, so it still works with
      the breaker tripped.
    - **Entries with an arxiv.org URL but no DOI** are unreachable by
      the current chain, which is DOI-keyed throughout. bibtui
      matches `arxiv.org/(abs|pdf)/<id>` on the entry URL as well as
      the DOI.
    - **One round-trip instead of two.** `extract.py:642` already
      turns an arXiv ID into a `10.48550/arxiv.*` DOI, so we hold the
      ID before we ask anyone anything.


- **Telling Claude which paper you mean.** ("Drag and drop"
  papers/references/cards into claude.) Designed 2026-08-31: in the
  embedded terminal there is no way to point at a paper, so you
  type a title fragment and hope `search_library` disambiguates —
  wordy, and worse when a sentence names two papers ("compare X
  with Y"). Three approaches, not exclusive:

    1. **Drag a card into the terminal.** A `Gtk.DragSource` on the
       card offering `text/plain` (the idiom is already used for
       PDB chips), a `Gtk.DropTarget` on the VTE widget, and
       `Vte.Terminal.feed_child()` to insert at the cursor —
       verified present in this build. Precise, composable
       mid-sentence, unambiguous.
       - **What to insert:** the DOI when there is one, else
         `paper:<id>`; both resolve through existing MCP tools
         (`find_by_dois`, `get_papers`). DOI-first because it is
         portable and greppable — but 28 of 172 papers here have
         none, so the id fallback is not optional.
       - A modifier-drag could insert the **PDF path** instead:
         Claude Code reads PDFs directly, so "this file" and "this
         paper's identity" are genuinely different requests.
    2. **A shared "what I'm looking at" channel.** The GUI already
       tracks card focus (`_mark_focus`) and knows what the viewer
       has open. Record it in a small table and expose an MCP tool
       (`current_papers()`), and "summarise this paper" works with
       no typing or dragging at all. Fits the architecture — both
       sides already share the database, so no new IPC.
       - **The catch is staleness:** "this" means something else if
         the GUI was last touched an hour ago. Return the
         timestamp so Claude can confirm ("the radiation-damage
         paper, selected three minutes ago?") rather than assume.
    3. **A "Copy reference for Claude" context-menu item.** Ten
       minutes' work, keyboard-friendly, and the fallback when
       dragging is awkward (second monitor, tiling WM). Falls out
       of (1) for free.

  **Order:** (1) first — the complaint is about naming a
  *particular* paper, sometimes more than one, and dragging is
  unambiguous in a way that "this" never is. Then (2) for the
  common case of "the one I'm reading".

- **GUI goes laggy/unresponsive during bulk import — measure, then
  restructure.** Observed 2026-08-29 importing a ~180-PDF folder;
  it is the first thing a new user with 100 PDFs would see. The
  import runs on background *threads*, which only helps while the
  work releases the GIL. Suspects, in order of likely guilt:

    1. **GIL starvation.** Extraction (pypdf is pure Python and
       holds the GIL solidly; the pdftotext/poppler bindings are C
       but hold it through long calls), thumbnail rendering
       (poppler+cairo), and regex-heavy parsing all starve the GTK
       main loop while a worker chews each PDF. A hundred PDFs is
       a hundred consecutive starvation windows.
    2. **Reload churn.** Every finished import pokes the debounced
       reload; through a long storm the main thread repeatedly
       rebuilds ~200 Pango-heavy cards while also GIL-starved.
    3. **SQLite contention** (upsert + FTS writes vs GUI reads;
       busy_timeout turns collisions into main-thread stalls) —
       real but probably secondary.

  **DIAGNOSED AND LARGELY FIXED 2026-08-30.** Both guesses above
  were wrong, and so was a first round of measurement that blamed
  `_reload` (17 s per rebuild) — that number came from a harness
  whose own startup reconcile was saturating the machine. Measured
  honestly against the real 197-paper library, every widely-blamed
  component is fast:

    | component | measured |
    |---|---|
    | build all 198 cards | 0.5 s (3 ms/card) |
    | render them in a window | 1.2 s to idle |
    | `_reload` remove-all + rebuild | 0.06 s + 0.53 s |
    | pdftotext + pdftoppm, 25-page PDF | 0.09 s |
    | poppler render, worst figure page | 0.38 s |

  The real cause, caught by a watchdog thread sampling the *main*
  thread's stack during the freeze (the technique to reuse next
  time — guessing cost hours, this took minutes):

    - **The viewer parsed the bibliography synchronously in
      `PdfViewerWindow.__init__`** (`bibliography_positions` →
      `parse_bibliography` → `_page_lines`). ~0.5 s alone.
    - **One dropped PDF was imported three times concurrently** —
      the drop handler plus the watcher's CREATED and
      CHANGES_DONE_HINT events. Each pass runs `_build_record`
      (pdfx/pypdf, pure Python, 1.7 s alone → 6.5 s under
      contention) and holds the GIL.
    - Together: the viewer's poppler work crawled between GIL
      slices, stretching 0.5 s into a **43 s** freeze. Total
      main-loop stalls for one import: **76.3 s**.

  **Fixed** (commit below): imports of the same path de-duplicate
  via an in-flight registry in `importer` (later arrivals wait,
  then take the cheap existing/recent path), and the viewer builds
  its citation links on a worker thread via the new module-level
  `viewer.build_citation_links`. Re-measured: **4.4 s total stalls,
  worst 1.8 s** (startup, not import), no watchdog reports at all.

  **Still open, in value order:**
    - **`_build_record` is the remaining GIL hog** (1.7 s per PDF
      of pure-Python pdfx/pypdf work). Since 2026-08-30 it runs
      behind `importer._extract_gate`, a one-permit semaphore, so
      it is no longer a contention storm — but it is still 1.7 s of
      held interpreter per PDF, and a 100-PDF first run still pays
      it serially. The out-of-process worker pool is the fix if
      throughput ever matters.

    *(The other two entries here — `refresh_valid_pdb_id_cache`'s
    weekly 57 MB freeze and `_do_drop_import`'s cross-thread
    connection — were both fixed on 2026-08-30/31 and have been
    removed. See `pdb_mentions._refresh_worker` /
    `_parse_entries_idx` and `browse._do_drop_import_with_conn`.)*
    - Incremental card updates / list virtualisation: NOT needed
      for responsiveness on today's evidence. Revisit only if a
      much larger library makes the 0.6 s rebuild matter.

  Measurement scripts (session scratchpad, recreate freely — the
  numbers and the watchdog technique are the durable parts):
  perf_card.py, perf_render.py, perf_rebuild.py, and
  perf_import_hang.py (stall detector + main-thread stack sampler).

  Related but separate *policy* question (not the perf fix):
  should the importer/watcher recurse into subdirectories at all,
  or import only the catalogue's own directory, with subdirs
  becoming catalogues of their own? Ties into the
  multi-directory-libraries item below; deciding it "flat" would
  also shrink the blast radius of bulk drops like `ramarota/`.

- **Supplementary-information PDFs: link to the parent paper, "SI"
  cartouche.** (Designed 2026-08-29 against
  `13321_2020_429_MOESM2_ESM.pdf` — currently an empty-metadata
  orphan card whose parent, AutoGrow4 `10.1186/s13321-020-00429-4`,
  is sitting right there in the library.)

    - **Detection:** filename markers (`MOESM\d+_ESM` Springer/BMC,
      `_si_\d+` ACS, `.sapp` PNAS, `suppl(ementary)?` generic),
      reinforced by extraction (the specimen's extracted title is
      literally "Supplemental Information"). Composes with the
      SI note already under the filename→DOI item.
    - **Parent resolution ladder:** (1) *library first, no
      network* — derive the DOI pattern from the filename
      (`13321/2020/429` → `s13321-020-00429-%`) and match against
      the papers table; (2) Crossref/OpenAlex when the parent
      isn't local; (3) manual "Attach to paper…" reusing the
      edit-dialog Find-metadata candidates search, with the pick
      setting the parent link instead of filling fields.
    - **Data model:** one sidecar field on the SI record only —
      `si_of: {doi, title}`. The parent stores nothing; its
      "has supplements" view is a card-build-time DB query (same
      trick as the in-library badges). Survives renames, travels
      with sharing.
    - **UI:** an "SI" cartouche à la PRE (template
      `make_preprint_badge`); on the SI card a clickable
      "Supplement to: <parent title>" line (`_navigate_to_doi`
      already exists); on the parent card a small "+N SI"
      affordance listing and opening its supplements.
    - **Exclusions:** SI records skip OpenAlex enrichment, JATS
      fetch and the citation refresher — no DOI of their own, so
      today they only waste calls and show as confusing empty
      cards. Stay indexed and searchable.

- **BUG: "refresh" destroys a hand-entered DOI, and there is no path
  from "I know the DOI" to "fetch the metadata".** Reported
  2026-08-27 against
  `1-s2.0-S0022283695800379-molecular-recognition-of-receptor-site.pdf`
  (Jones, Willett & Glen, JMB 1995). The user pasted the DOI into the
  metadata dialog and hit refresh expecting OpenAlex to fill in the
  rest. Nothing happened, and the sidecar still reads `doi: null`.

  `importer.refresh_pdf` rebuilds the record with
  `_build_record(pdf_path)` — a fresh extraction from the PDF — and
  then restores a fixed list of user-curated keys: tags, notes, mark,
  hand_edited, added_date, citations*, auto_keywords, abstract,
  authorships, highlights, published_version, bibtex_*. **`doi`,
  `title`, `authors`, `year` and `journal` are not on that list**, so
  anything typed into those fields is overwritten by whatever the PDF
  yields — for this paper, nothing.

  It is a lose-lose, because the escape hatch does not help either:

    - `hand_edited` unchecked → refresh wipes the typed DOI.
    - `hand_edited` checked → `refresh_pdf` returns early with status
      `hand_edited` and skips the record *entirely*, so no enrichment
      runs at all.

  There is no combination that does what the user asked for. Two
  fixes, and they are independent:

    1. **Preserve a hand-entered DOI across refresh.** At minimum add
       `doi` to the preserved-keys list when the incoming extraction
       has none — a typed DOI is strictly better information than an
       absent one. (Note `do_save`'s comment claims it "auto-sets
       hand_edited if the user actually changed anything"; the code
       only honours the checkbox. Either fix the comment or implement
       what it says — but see the lose-lose above before making
       hand_edited automatic, or typing a DOI would start silently
       disabling enrichment.)
    2. **Add an explicit "Fetch metadata from DOI" button to the edit
       dialog**, next to the DOI entry. This is the affordance the
       user reached for and it does not exist. It should call
       `metrics.fetch_work_by_doi` on the entry's current text and
       populate title / authors / year / journal in place, before
       save — so the result is visible and correctable rather than
       applied blind. "Refresh" meaning "re-read the PDF" is
       defensible; it just is not discoverable as *not* meaning "go
       and look this up".

- **Derive DOIs from publisher filename conventions.** Download
  filenames are not noise — most publishers encode an identifier in
  them, usually the DOI with the punctuation filed off. Worth trying
  whenever extraction yields no DOI. Deterministic transformations,
  each verified to resolve against a file in the library:

    - **Elsevier PII** — `1-s2.0-S0022283695800379-…` (and the
      extracted title `PII: S0022-2836(95)80037-9`) → `10.1016/` +
      PII lowercased, punctuation kept →
      `10.1016/s0022-2836(95)80037-9`. Rescues older ScienceDirect
      downloads, exactly the ones whose PDFs carry no DOI.
    - **arXiv** — `2006.11239v2.pdf` → `10.48550/arXiv.2006.11239`
      (strip the version suffix).
    - **RSC** — `d1ob00658d.pdf` → `10.1039/d1ob00658d`.
    - **bioRxiv** — `2024.04.29.591684v1.full.pdf` →
      `10.1101/2024.04.29.591684`.
    - **Nature/BMC** — `s41467-025-56045-z.pdf` →
      `10.1038/s41467-025-56045-z`.
    - **Science/AAAS** — `science.aef8874.pdf` →
      `10.1126/science.aef8874`; `sciadv.abg3338` likewise.
    - **ACS** — `acs.jcim.4c02293.pdf` → `10.1021/acs.jcim.4c02293`.
    - **PNAS** — `pnas.0502225102.pdf` → `10.1073/pnas.0502225102`.

  **Measured on the current library (166 papers, 38 without a DOI):
  about 14 of the 38 carry a decodable filename** — roughly a third.
  The whole-library figure is misleading by comparison: ~36 filenames
  decode, but 34 of those already got a DOI from the PDF text. The
  value is concentrated exactly where extraction failed, which is the
  point.

  **Supplementary files need different handling.** Several decodable
  ones — `42004_2020_367_MOESM1_ESM`, `41467_2025_67297_MOESM3_ESM`,
  `jm5c03004_si_001`, `pnas.2524504123.sapp` — are supporting
  information, not papers. Decoding yields the *parent* article's
  DOI, which is more useful than nothing but must not create a second
  record for a paper already in the library. Treat a recognised
  SI / ESM / sapp marker as "attach to parent", not "this file is
  that paper".

  **IUCr is the gap, and the one that matters most here.** A large
  cluster of the DOI-less files are IUCr article codes — `ba5248`,
  `gr2280`, `st0606`, `be0003`, `li0224`, `hr0021`, `a25349`,
  `a63832`, `nsb0498-266b`. They appear in journals.iucr.org URLs but
  there is no arithmetic from code to DOI, so they need a lookup
  (Crossref/OpenAlex search on the code, or an IUCr endpoint) rather
  than a transformation. Given how much crystallography sits in a
  library like this, it is the highest-value publisher to support and
  the only one on the list that is real work.


- **Resolve metadata from first author / journal / year when there is
  no DOI.** The natural companion to the pasted-citation work under
  Discovery — `find_doi_by_author_year` already does this and is
  measurably good. Honest caveat from the case that prompted it,
  though: it would *not* have rescued this particular PDF, because
  the sidecar has `authors: []`, `journal: null` and `year: 2005`.
  There was nothing to resolve *from*. The author/journal/year route
  works when the extractor got those fields and missed only the DOI;
  the PII route above is what saves this one.

- **ResearchGate as a PDF source — considered and rejected
  (2026-08-27).** Asked whether having an RG account would let us
  fetch PDFs, by API or scraping. No, on three independent grounds,
  any one of which is sufficient:

    - **There is no API.** ResearchGate has never offered public
      programmatic access. Every "ResearchGate API" on offer is a
      third-party scraper wrapper, not an authorised interface.
    - **The ToS prohibits it in as many words** — users may not
      "employ any mechanisms, devices, software, scripts, robots, or
      any other means or processes (including crawlers, browser
      plugins, add-ons, or any other technology) … to access, scrape
      or copy content, data, or profiles". `robots.txt` looks
      permissive on paths, but robots.txt is not a licence and does
      not override the ToS; the `Disallow: /cdn-cgi/` line also
      confirms Cloudflare in front, which blocks automation in
      practice regardless.
    - **Having an account makes it worse, not better.** An account
      means the ToS has been accepted personally, so automated access
      puts *that* account at risk rather than being anonymous
      misbehaviour.

  **The 2023 settlement also changed what is even there.** ACS and
  Elsevier sued ResearchGate in Germany (2017) and the US (2018); a
  Munich court held RG responsible for infringing uploads in 2022,
  and the parties settled in September 2023. The remedy was automated
  copyright checks *at upload* for ACS and Elsevier content, with
  affected articles stored **privately** and shared only on
  individual request. So the full texts a scraper would target are
  increasingly not public at all — and among those that are, many are
  author uploads that breach the author's own publisher agreement, so
  a "successful" fetch can be an infringing copy.

  **What is legitimate, and worth doing instead.** RG's "Request
  full-text" is a human-to-human flow — the author is asked and sends
  a copy. Alexandria can support that without automating anything, in
  the same shape as the proposed EZproxy affordance: an "Open on
  ResearchGate" action on a card with no OA copy, which opens the
  browser and stops. User-initiated, no scraping, no ToS problem. The
  real answers to "get me this PDF" remain the OA path we already
  have (OpenAlex / Unpaywall), EZproxy for subscribed content, and
  the inside-the-firewall waiting list.

- **Do not take the publication year from the PDF CreationDate.**
  Same sidecar: `year: 2005`, from `CreationDate D:20050105…`, for a
  paper published in 1995. Elsevier re-exported its back catalogue in
  the mid-2000s, so every scanned-era PDF of an older paper carries a
  creation date a decade or more after publication. A year that
  disagrees wildly with a resolved DOI, or that post-dates a PII's
  encoded year (the `(95)` in `S0022-2836(95)80037-9` *is* the year),
  should be treated as unknown rather than asserted.

- **Collect and store JATS XML alongside PDFs** — consolidated into
  the dedicated **"Structured full text (JATS) instead of PDF
  archaeology"** section below, which carries the full case,
  on-disk shape and sequencing.

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
- **Import by PMC / PubMed identifier**, alongside import by DOI.
  Asked for 2026-09-03. Paste `PMC1234567` (or a bare PMID) and get
  the paper, exactly as `doi_import_dialog.open_doi_import` does for
  a DOI.

  **Most of it exists.** `jats.pmcid_for_doi` already talks to the
  Europe PMC search API to go DOI → PMCID; the same endpoint answers
  the other direction, and its result carries the DOI, title,
  authors, journal and year. So the work is: resolve the identifier
  to a DOI, then hand off to the path already built —
  `metrics.resolve_doi` → `bibtex_import.br_from_metadata` →
  `import_record`. The dialog itself is `doi_import_dialog` with a
  different `_clean_doi` and a different first hop.

  **Why it is worth more than a second way to type an identifier.**
  A PMC identifier *means* the full text is deposited and open. So
  unlike a DOI import, this one can reliably also fetch the JATS
  (`jats.fetch_and_store`, already written) and stands a much better
  chance of finding an open-access PDF. A PMC import could arrive
  complete — metadata, full text, references with DOIs, and the PDF
  — where a DOI import often arrives as metadata alone.

  Details worth settling:
    - **Accept all three spellings.** `PMC1234567`, `1234567` with
      the PMC prefix implied, and a bare PMID (no prefix, different
      number space). A PMID and a PMCID are easy to confuse and the
      wrong guess silently imports the wrong paper, so a bare number
      is ambiguous: either ask, or query both and say which matched.
      `europepmc.org/article/MED/<pmid>` vs `/PMC/<pmcid>`.
    - **A full Europe PMC URL pasted from the browser** should work
      too, the way a DOI URL does.
    - **No DOI at all.** Some PMC records (older, or preprints) have
      no DOI. The DOI path assumes one throughout — dedup, ghost
      creation, JATS — so decide whether to refuse those or to key
      the ghost on the PMCID.

  Same dialog, so it should share the "also fetch the PDF" checkbox
  and the ghost/duplicate handling rather than growing a parallel
  implementation.

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
- **Resolve a pasted citation string ("Jones et al., J. Mol. Biol.
  1995").** LLM answers, emails and talks hand out references in
  loose prose form. We can already resolve these — the gap is a front
  door, not machinery:

    - `metrics.find_doi_by_citation` (Crossref `query.bibliographic`,
      with a first-author-surname gate) handles strings that carry a
      title.
    - `metrics.find_doi_by_author_year(surname, year, journal)` was
      built for the no-title case and already does journal-
      abbreviation expansion (`_JOURNAL_ABBREVIATIONS`) and year
      tolerance.

  Measured: "Jones / J. Mol. Biol. / 1995" resolves to the GOLD
  docking paper, "Jones / Acta Cryst. A / 1991" to the O
  model-building paper, "Sankararaman / Nature / 2014" and "Jumper /
  Nature / 2021" both correct. Note the title-bearing path is the
  *weaker* one — the full AlphaFold citation returned None because
  Crossref's top hit had a different first author and the guard
  rightly rejected it, while the author/year/journal path got it.

  **What is missing:** a string parser (surname / year / journal /
  title-words) and a UI entry point — a fifth Discover tab is the
  obvious home, since `_build_work_row`, the Add-to-library path and
  the author field are all reusable.

  **Show a ranked list, not one answer.** Google Scholar does not
  disambiguate these either; it ranks well enough that the top row is
  the wanted one, and that is the behaviour to copy. "Jones / JMB /
  1995" has 21 candidates — atypical, needing a common surname *and*
  a huge journal *and* the year window, but real. Ranking measured
  over three queries:

    - **First-author-surname match, then citations descending.**
      Neither works alone: plain citation sort promotes papers where
      the surname is a *co*-author (Tomlinson, Kung and Ståhlberg all
      outranked genuine first-author Jones papers), and first-author
      filtering alone leaves six candidates in arbitrary order.
      Together the wanted paper was #1 in every case tested.
    - **The citation gap between #1 and #2 is a free confidence
      signal** — 12x for Jones/JMB/1995, 577x for Jones/Acta A/1991.
      A large gap means "resolved, alternatives below"; near-parity
      means "genuinely ambiguous, pick one". No threshold tuning, and
      the alternatives are never hidden.
    - Do not rely on OpenAlex's default ordering for a filter-only
      query: it is not citation-ranked (positions 2-6 of the Jones
      query ran 124, 116, 200, 166, 81), so today's single-answer
      behaviour is partly luck.

  **Worth exposing as an MCP tool too** (`resolve_citation`): a
  reference that resolves to *nothing* is a useful signal in itself —
  "this citation may not exist as stated" — which turns the feature
  into a hallucination check on LLM-supplied references rather than
  only a convenience.

- **Fix the year window in `find_doi_by_author_year` — affects
  behaviour today, not just the feature above.** The viewer's
  reference popover falls back to this path for Acta-Cryst-style
  entries that carry no title, and those are often old papers. The
  current window is a symmetric +/-1 year, justified in the code
  comment by online-vs-print drift. That justification is
  era-specific and the window is wrong in two ways:

    - **No drift exists before electronic ahead-of-print publication**
      (roughly pre-1997 — ACS ASAP ~1996, ScienceDirect 1997, staggered
      by publisher, so the cutoff is a judgement call; being
      conservative costs little). For those years the window is pure
      noise, and it is exactly where it hurts most: Jones / JMB / 1995
      goes from 21 candidates (6 first-author) at +/-1 to **8 (3) at
      exact year**, losing nothing — the GOLD paper is 1995 exactly.
      Jumper / Nature / 2021 likewise drops 8 to 2.
    - **The modern window should be asymmetric, `[year-1, year]`.**
      The drift only runs one way: the citation carries the later
      print year while OpenAlex records the earlier online date.
      Verified on the case the comment itself cites — "A short
      history of SHELX", cited as Acta Cryst. A64 (2008), has
      `publication_date: 2007-12-21` in OpenAlex. `year+1` was never
      justified by that failure mode.

  Edge case to keep in mind: an old citation occasionally carries the
  *volume* year rather than the issue year, and those disagree for
  journals whose volumes straddle a year boundary. Exact matching
  misses those — so fall back to dropping the year filter entirely
  when an exact-year search returns nothing, and let the ranked list
  sort it out.

  **Regression fixture — the Jones/GOLD pair.** Two papers, same
  first author, same journal, two years apart, and the wrong one is
  4x more cited. This is the exact shape that makes citation ranking
  confidently wrong when the year window is loose:

    - `Jones, J. Mol. Biol. 1995` must resolve to
      **10.1016/s0022-2836(95)80037-9** — Jones, Willett & Glen,
      "Molecular recognition of receptor sites using a genetic
      algorithm with a description of desolvation", 1,548 cites.
    - `Jones, J. Mol. Biol. 1997` must resolve to
      **10.1006/jmbi.1996.0897** — Jones, Willett, Glen, Leach &
      Taylor, "Development and validation of a genetic algorithm for
      flexible docking" (GOLD proper), 6,752 cites.

  Under the current symmetric +/-1 window the 1995 query has both
  papers in range, and first-author-then-citations ranking puts the
  1997 paper on top — the wrong answer, delivered with a large
  citation gap that reads as high confidence. Exact-year matching
  fixes it. Assert both directions: the 1995 query is the one that
  fails today, and the 1997 query guards against over-correcting.

  (Incidental trap for anyone tempted to shortcut: the 1997 paper's
  DOI suffix says `1996`. Publication year cannot be read off a
  DOI.)

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
    - **Publisher thumbnails on feed cards.** Feed articles have no
      PDF, so their cards have no thumbnail — the one place in the
      app where that slot is genuinely empty (library cards render
      the real first page, which is better and stays). Publisher
      landing pages carry a graphical abstract we can use: on IUCr,
      `journals.iucr.org/paper?<doi-suffix>` has
      `<div class="thumbinmain"><img src="…rl5208thumbnail.gif">`.
      Decided 2026-08-30:
        - **IUCr first, behind a per-publisher dispatch table** in
          the shape `funding_links.funding_url` already uses for
          funders — so Springer/Wiley/etc. are one function each
          later, with no restructuring. Not OpenGraph-first:
          `og:image` is usually a logo or social banner, not the
          paper's own graphic.
        - **Cache in `<library_root>/.feed-thumbs/`**, keyed by
          DOI, matching the existing `.author-images/` convention:
          travels with a synced library, one place to delete.
        - **Feed cards only.** Ghost library cards keep their
          generic icon for now (same fetcher would serve them —
          one extra call site — if that gap starts to itch).
      Fetch lazily on a worker thread when a card is built; never
      block, and a miss just leaves the slot empty. Needs the polite
      per-host throttling the publisher-page-harvest item describes
      (robots.txt, ~1 req/host/sec) since this scrapes HTML.

    - **Feed cards: hover-to-read the full abstract.** The
      truncated abstract line should show the complete text on
      mouse-over (tooltip, like the works-row author line in the
      Authors window) so reading it doesn't require View.
      *(Markup rendering — the other half of this item, noted
      2026-08-29 — shipped 2026-08-30: feed titles/abstracts go
      through `safe_pango_markup`, which now also translates
      namespaced JATS and collapses the pretty-printed XML
      whitespace Crossref ships.)*
    - **Search in the Subscriptions window.** A search entry over
      the discovered-article cards (title + authors + journal +
      abstract), client-side over `discovered` rows like the
      pill filter — find that half-remembered paper without
      scrolling the whole feed. Natural first slice of the
      custom-feed-filters item below (same tokenised matching,
      no persistence).
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

- **Authors should live alongside the catalogues, not inside them.**
  Realised 2026-09-02, working out from two smaller problems: author
  avatars are stored per library root, and "which of this author's
  papers do I have" only knows about the current catalogue. The
  common cause is that *everything* about an author is currently
  filed under whichever catalogue happened to be open.

  **The evidence that this is the wrong home: none of it is
  catalogue-specific.** Four tables, all keyed by a global identity
  and holding nothing that belongs to one library:

    - `author_trail` — key (OpenAlex ID, else ORCID), name,
      institution, position, added_at, last_viewed
    - `author_scores` — citing impact per OpenAlex ID
    - `author_works_cache` — that author's works from OpenAlex
    - `author_relations` — who cites them (added 2026-09-01)

  Plus `<library_root>/.author-images/`, already named by the same
  global key (see the avatars item in Sharing). A person's identity,
  their photograph, their publication list and who cites them do not
  change because you switched catalogue. Only *which of their papers
  you hold* does — and that is the one thing that should stay
  per-catalogue.

  **The case, walked through (2026-09-04).** Start Alexandria; it
  opens the catalogue you last used, say `moorhen`. Open `default`
  from the hamburger menu — a second window. Open the Authors window
  from *that* one. Now click an author in a popover on a card in the
  original `moorhen` window. What happens:

    - `author_works._ensure_window` is a module-level singleton and
      uses its `conn` argument **only when creating** the window. It
      already exists, so `moorhen_conn` is discarded.
    - `show_author` calls `index.add_author_trail(self.conn, …)`, so
      that author joins the **default** catalogue's trail.
    - The page then resolves everything against default:
      `_existing_dois(self.conn)` means a paper you hold in moorhen
      shows as *not* held, "Add to Archive" would import into the
      default library, and the citing-authors cache writes to the
      default database.
    - Avatars are worse, because they do not follow the window at
      all. `author_image.image_path()` uses
      `prefs.get_library_root()` — the *globally* current catalogue.
      Opening the default window called
      `set_current_catalogue("default")`, so avatars resolve to
      default's directory even for the moorhen window's own cards.
      This is why photos appear to "disappear": 16 images under
      default, 4 under moorhen, and which set you see depends on
      what you opened last rather than on which window you are in.

  Nothing crashes and nothing is corrupted. Every answer is
  plausible and silently attributed to the wrong catalogue — the
  same species as the MCP server serving one catalogue's rows
  against another's paths (fixed in 084c759): one half follows the
  catalogue and the other half does not.

  With a shared authors database and a shared image store, every
  question above stops being ambiguous. There is one trail, one set
  of photos, and the only per-catalogue thing left is "do I hold
  this paper" — which is the one question that genuinely differs.

  **Tractable, because the coupling is thinner than it looks.** The
  one place the two sides meet is `index.stale_author_score_ids`,
  which picks authors to refresh from the papers in the library —
  and it already collects the IDs in *Python* from
  `authorships_json` before joining them against `author_scores`
  through a temp table. So the paper side can come from a catalogue
  connection and the join move to an authors connection, without
  restructuring the query.

  **The tricky parts, in order:**
    - **Merging existing trails.** Each catalogue has its own trail
      with its own `position` ordering; a union has to reconcile
      those. `last_viewed` merges as a max, `added_at` as a min,
      duplicates collapse on the key — but the ordering is a real
      decision, and a user who curated two trails will notice.
    - **Signatures ripple.** `AuthorPage` and `AuthorsWindow` take a
      single `conn`. They would need an authors connection *and*, for
      the papers-you-hold section, catalogue connections. That is
      the change that touches the most code.
    - **Per-host naming.** The library database is already
      `library.<host>.db` because a home directory can be shared over
      NFS (see Multi-host / NFS). An authors database needs the same
      treatment, or two machines will corrupt each other's trail.
    - **Deleting a catalogue.** The trail would survive it. That is
      probably right — you were following that person, not that
      library — but it should be a decision.
    - **Self-containedness**, the same doubt as the avatars item: a
      catalogue directory stops carrying everything about itself.

  **Do the avatars first.** They are the same argument at one tenth
  the size, they are already globally keyed, and they have a single
  choke point in `author_image._images_dir`. Getting the shared
  location and its XDG/Flatpak questions settled on the small case
  makes the big one mostly mechanical — and a shared authors
  database is the natural companion to a shared image store, and to
  the avatar packs below.

- **"Papers in the library" section in the author page.** Asked for
  2026-09-02: on an author's page, show which of their papers you
  already hold — as a section alongside "Frequent collaborators" and
  "Cited most often by", and collapsible like them.

  **The detection already exists.** `author_works._existing_dois(conn)`
  builds the set of DOIs in the catalogue, `_make_work_row` marks a
  row `in_library` from it (`author_works.py:1618`), and
  `refresh_in_library` keeps it current as papers are imported. So
  the answer is already computed and scattered down the works list;
  this is about collecting it into one place at the top, where "do I
  have this person's work?" is answered without scrolling a
  200-entry list.

  Fits the shape `_section_expander` / `_people_flowbox` now
  establish, but the rows want to be papers rather than name chips —
  title, year, journal — and clicking one should open the PDF, which
  the works list can already do.

  **Across catalogues: worth doing, but it is a different feature.**
  `prefs.get_catalogues()` and `index.db_path_for_catalogue(name)`
  make the query straightforward — open each catalogue's database
  read-only and match on DOI. The awkward parts are not the query:

    - **What does clicking a result do?** A paper in another
      catalogue cannot be opened in this window. It means switching
      catalogue (the `[open-catalogue]` path browse.py already has)
      and landing on that paper — a bigger interaction than "open
      the PDF", and one that discards the window you were in.
    - **Catalogues can be absent.** A library root on an unplugged
      drive or an unmounted share must degrade to "not counted",
      not to an error or a hang on a stat.
    - **N databases on a page load.** Opening every catalogue's
      database to draw one section is the kind of thing that has
      bitten this app before; it belongs on the worker thread that
      already fetches the profile, with the count arriving late like
      the citers row does.
    - **Say which catalogue.** "3 papers, 2 here and 1 in moorhen"
      is useful; a bare total is not.

  Suggest shipping the current-catalogue version first — it is
  mostly rearranging what is already computed — and treating the
  cross-catalogue count as a follow-on once the section exists and
  its usefulness is obvious.

- **"This author cites" author list in the author dialog.** The
  companion to the "Cited most often by" row shipped on 2026-09-01
  (see below): who this author *reads*, as against who reads them.

  **The cost estimate that was here was wrong, by about fifty-fold.**
  It assumed fetching `referenced_works` for each of A's works and
  then the authorships of each of those — "~30–50 calls for a typical
  career". OpenAlex will do the whole tally server-side. Measured
  2026-09-01 against Jon Agirre (89 works, 1,783 referenced works):

      /works?filter=cited_by:W1|W2|…,authorships.author.id:!A<id>
             &group_by=authorships.author.id

  **Two calls**, one for the work IDs and one for the tally — the
  mirror image of the `cites:` query the "cited by" row already uses,
  with `cited_by:` in place of `cites:`. It returned Paul Adams,
  Garib Murshudov, Randy Read, Gideon Davies at the top, which is
  the right answer.

  Almost all of the work is done: `metrics.rank_citing_authors` and
  `metrics.MAX_CITES_OR_IDS` apply unchanged, `author_relations`
  needs one more column (`cites_top_json`) beside `cited_by_top_json`,
  and `author_works._apply_citers` is a copy with a different label
  and tooltip. Half a day at most.

  Caps to keep in mind, both learned building the other half:
  OpenAlex rejects more than 100 OR'd IDs outright rather than
  truncating, and batching beyond that double-counts a paper that
  spans two batches — so use the author's 100 most-cited works and
  say so, rather than pretending to completeness.

  **Why this beats the graph for v1:** much higher
  information-density per pixel, no layout problem to solve, and the
  social signal — "Cowtan cites Sheldrick and Murshudov; is cited by
  [young protégés]" — is the part of a citation graph that actually
  carries meaning to a researcher.

  Defers the Cairo citation-graph item below: ship this first,
  see whether the graph still feels missing afterwards.

  - *(Shipped 2026-09-01: the "Cited most often by" half. The row
    sits under Frequent collaborators in `author_works.py`, names
    are clickable through to that author's own page, and the ranked
    list is cached 30 days in the new `author_relations` table.
    `metrics.fetch_top_citing_authors` is the two-call fetch;
    `index.db_path_of` was added so the worker can open its own
    connection from a window that is handed only a connection.)*

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

- **Author avatars are per-catalogue, and should not be.** Noticed
  2026-09-02: every catalogue keeps its own
  `<library_root>/.author-images/`, so a photo fetched while
  browsing one library is invisible from another. Measured on a real
  machine: 16 images under the default catalogue, 2 under `moorhen`,
  0 under `testing` — three disjoint sets, and the same person
  fetched twice would be stored twice.

  **The hard part is already solved.** Files are named
  `<index.author_trail_key(authorship)>.png` — OpenAlex ID, else
  ORCID — which is a *global* identity, not a catalogue-local one.
  So the same author has the same filename in every catalogue on
  every machine; the only thing wrong is which directory it sits in.
  There is no migration to design, only a move and a lookup order.

  Options, roughly in order of appetite:
    1. **A shared store outside any library**, e.g.
       `~/.local/share/Alexandria/author-images/`, with
       `author_image._images_dir()` pointing there. Simplest, fixes
       it everywhere, and the images stop being part of a library's
       on-disk footprint.
    2. **Shared store, with per-catalogue override** — read the
       library-local directory first and fall back to the shared
       one, so a catalogue can still carry its own picture of
       someone. More moving parts; unclear anyone wants it.
    3. **Leave them local and sync**, which is really option 1 with
       extra steps.

  **Option 1 is the preference (2026-09-02), with four things to
  settle first:**

    - **A library stops being self-contained.** Today
      `~/Documents/Alexandria` holds the PDFs, the sidecars *and*
      the photos, so copying that one directory to another machine
      or restoring it from backup brings everything. Move the images
      out and it no longer does — which is an odd result for a
      change whose whole purpose is to make them travel better.
      Either the export/backup story has to name both locations, or
      a library keeps a copy on export (which is what the
      avatar-packs item below is for).
    - **Flatpak sees a different `~/.local/share`.** The sandbox
      redirects it to `~/.var/app/io.github.pemsley.Alexandria/data/`
      unless the manifest says otherwise; `flatpak-notes` currently
      grants only `--filesystem=xdg-documents`. So a Flatpak install
      and a `pip install --user` one would each build their own
      "shared" store and never see each other's. Decide whether
      that is acceptable or whether the manifest gains a permission.
    - **Data or cache?** `~/.cache` is the usual home for anything
      re-fetchable, and a Wikidata photo is. But a photo the user
      *chose* — dragged from a browser, picked from a file — is not
      recoverable, and the two sit in the same directory under the
      same name. That argues for `XDG_DATA_HOME` (i.e.
      `~/.local/share`), and for never treating the directory as
      disposable. Use `XDG_DATA_HOME` rather than hard-coding the
      path, as `browse.py` already does for `XDG_STATE_HOME`.
    - **Nothing prunes it.** A per-library directory goes when the
      library does; a shared store accumulates images for authors
      no catalogue references any more. Probably acceptable — they
      are keyed by a stable identity and each is ≤512px — but it
      should be a decision rather than an oversight.

  Whichever way, `_images_dir(root=None)` is the single choke point
  — every read and write already goes through `image_path` — so this
  is a small change plus a one-off migration that moves existing
  files up and de-duplicates by filename.

  **Decide alongside the avatar-packs item below**, which is the
  same question asked across machines rather than across
  catalogues: a shared store is also the natural thing for a pack to
  install into, and for a Syncthing/Drive folder to point at. Doing
  packs first would bake in the per-library assumption.

  Watch: the images are ≤512px PNGs, so a big collection is real
  disk (16 images is already ~2 MB). A shared store makes that one
  cost rather than N.

- **Author-avatar packs: export and import a collection wholesale.**
  "Here are my collected author avatars — apply this set to the
  authors in your database." Plausibly the most shareable artefact
  the app produces, because the effort is per-person and the result
  is reusable by anyone studying the same field.

  **Where images live today.** `<library_root>/.author-images/
  <key>.png`, where `<key>` is `index.author_trail_key(authorship)` —
  **OpenAlex ID, else ORCID** — normalised to a <=512px PNG. The path
  is deterministic, so there is no DB column to migrate. All 19
  images in the current library are OpenAlex-keyed; none are
  ORCID-keyed.

  **Key packs on the OpenAlex ID, with ORCID as a secondary
  matcher.** (Decided 2026-08-27, after first arguing for ORCID —
  the practical numbers go the other way.)

    - **Coverage.** Every image in the current set has an OpenAlex ID
      (19/19, since it is already the storage key); only **15 of 19
      (79%)** resolve to an ORCID. Keying on ORCID would silently
      drop a fifth of a collection.
    - **No resolution pass.** OpenAlex ID *is* `author_trail_key`'s
      first choice, so export is a straight copy. ORCID-keying would
      need a network round-trip per author at export time, and would
      still be incomplete.
    - **Both ends already speak OpenAlex.** Authorships come from
      OpenAlex via `metrics`, so both libraries in an exchange
      overwhelmingly hold OpenAlex IDs for the same person. The
      "keyed differently in two libraries" case is real but rare.

  ORCID still earns a slot in the manifest, because it costs nothing
  and covers exactly the cases the OpenAlex ID does not: an
  authorship carrying an ORCID but no OpenAlex ID (so `image_path`
  keyed on the ORCID), and an OpenAlex ID that has since been merged
  away during re-disambiguation. Record both; match on either.

  **This requires no change to `image_path`.** The local storage key
  stays exactly as it is and the *manifest* is the interchange
  format. Export writes each local key plus every other identifier it
  already knows; import maps a pack entry back to whatever key the
  receiving library uses for that author and writes under it. Nothing
  about `author_image.py`'s on-disk contract changes.

  **Pack format** — a zip/tar of images plus a manifest JSON. Each
  entry should carry *all* identifiers known, not one:

    ```json
    { "file": "A5014431830.png",
      "openalex_id": "A5014431830",
      "orcid": "0000-0002-1151-6227",
      "display_name": "...",
      "source_url": "...", "licence": "CC BY-SA 4.0",
      "fetched": "2026-08-27" }
    ```

  **Import matching order:** OpenAlex ID → ORCID → exact display
  name. The last is genuinely risky (the Discover author search
  already shows how many distinct people share a name) so it should
  be opt-in, and never silent.

  **Conflict handling.** An author who already has an image: default
  to *not* overwriting, mirroring the `hand_edited` principle
  elsewhere — a photo the user chose deliberately should survive
  applying someone else's pack. Offer overwrite as an explicit
  choice, not a default.

  **Licence: share pointers, not pixels.** These are photographs of
  real people, and bundling them up and passing them on is
  republication — an academic's photo on their institutional page is
  not licensed for redistribution merely by being public. Measured on
  the current collection (2026-08-27), that is not a corner case, it
  is the whole thing:

    - **1 of 19** avatars has a Wikidata P18 portrait, where licence
      and attribution come free from the Commons `extmetadata` API
      with no user interaction at all.
    - **18 of 19** were hand-dropped from web pages — unknown
      licence.
    - The originals still on the Desktop carry only
      `com.apple.quarantine` (downloading app: Firefox) and **no**
      `kMDItemWhereFroms`, because Firefox does not write the
      where-from attribute Safari and Chrome do. So the source URLs
      for the existing set are **not recoverable retroactively**.

  A pixel-bundle export would therefore be ~95% images that should
  not be redistributed, which kills that form of the feature. The
  version that works is **a pack of pointers**: the manifest maps
  `OpenAlex ID → image source URL`, and import fetches each image
  locally. No redistribution, so no licence question arises — and the
  recipient still gets the entire benefit, because the labour in
  assembling a set is *finding* the portraits, not downloading them.
  Sharing that search result is the valuable part.

  Consequences for capture, and note that **nobody should ever be
  prompted to type a licence**:

    - When a **URL** is dropped, record it. That is the only capture
      that has to work, and it is free.
    - When a **local file** is dropped there is nothing to record —
      store `source_url: null` and accept that such entries simply
      cannot go in a pack.
    - When the image came from **Wikidata**, record the Commons file,
      licence, artist and `AttributionRequired` from `extmetadata` —
      also free, and it makes those entries shareable as actual
      pixels if that is ever wanted.

  Retrofitting is impossible, as the Firefox finding above
  demonstrates, so the recording is worth adding to `save_image`
  whenever it is touched — long before packs exist. The existing 19
  are already unrecoverable.


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
  zero entries — *and* the entries it does have are noisy in a
  way CrossRef's publisher-deposited funder block usually isn't.
  CrossRef's `/works/{doi}` carries a `funder: [{ name, DOI,
  award: [...] }]` block we currently drop. Pull it in
  `_fetch_crossref_work_message` as a fallback when the OpenAlex
  grants list is empty (and possibly as a *preferred* source when
  the publisher deposited it); map into the same
  `[{funder, award_id}]` shape we already persist. Composes with
  the aggregated PI funding profile item under Discovery.

  **Reference case study — `10.1007/s10822-015-9866-z` (WONKA).**
  The paper's funding paragraph lists two genuine grants —
  `EP/G037280/1` (EPSRC, to Bradley) and `092809/Z/10/Z`
  (Wellcome, to the SGC). OpenAlex's `awards` array produces:
  ```
  EPSRC                                    EP/G037280/1       ✓
  Wellcome Trust                           092809/Z/10/Z      ✓
  Wellcome Trust                           1097737            ✗  charity reg #, not a grant
  Ontario Ministry of Economic Dev.        092809/Z/10/Z      ✗  cross-attributed
  ```
  …plus a phantom `Ministero dello Sviluppo Economico` in the
  funder list (no Italian funding in the paper — likely a fuzzy
  substring match against "Ministry of Economic Development").
  Three failure modes in one paper:
    1. A non-grant numeric identifier from the body text scooped
       up as an award_id and pinned to the nearest funder.
    2. One real grant duplicated across multiple funders that
       happen to co-occur in the funding paragraph.
    3. A phantom funder added via fuzzy name match.
  CrossRef's `funder:` block doesn't do any of these — it stores
  exactly what the publisher deposited. Even if we keep OpenAlex
  as the data shape, taking CrossRef's `funder:` as ground truth
  for which `(funder, award_id)` pairs to display would eliminate
  all three classes of error on papers where CrossRef has the
  data.

## OpenAlex client
- **Author-view works cache: lengthen the TTL, and stop blocking on
  stale rows.** Requested as "cache the Author View publications so
  reopening tomorrow doesn't wait on OpenAlex". Most of that already
  exists — `index.author_works_cache` is keyed
  `(openalex_id, sort_key)`, lives in SQLite so it survives restarts,
  and already holds both the recent and cited lists (42 rows in the
  current library). Two things are genuinely missing:

    1. **Stop blocking on stale rows — this is the actual fix.**
       `_author_score_is_fresh` is documented as "anything older we'll
       show briefly then refresh" (stale-while-revalidate), but
       `_author_works_cache_is_fresh` says "stale rows are dropped on
       read so the next switch fetches anew" — so a stale works row
       means *waiting on the network*, which is exactly the symptom
       reported. Adopting show-then-refresh for works removes the wait
       **even at the current 7-day TTL**, and on a genuine miss too.
       Do this first: it fixes the complaint on its own.

    2. **Then lengthen the TTL** — a refinement, not the cure, since
       raising it alone only makes the wait rarer. `AUTHOR_WORKS_TTL_DAYS
       = 7` is short, and the code comment justifying it ("shorter than
       `author_scores` because the most-recent ordering picks up new
       publications faster than citation tallies move") is sound but
       **asymmetric**: it argues for a short TTL on the *recent* list
       only. The cited list barely moves in a month. Since `sort_key`
       is already in the primary key, a **per-sort-key TTL needs no
       schema change** — `cited` at 30 days (matching
       `AUTHOR_SCORE_TTL_DAYS`), `recent` shorter, say 14. For authors
       who publish a few times a year, even that is conservative.

  Reducing OpenAlex requests is a real but secondary benefit — the
  credit cost of re-fetching one author's two lists is small. The wait
  is the complaint, which is why the ordering above matters.


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

- **Mark related entries — the "Acedrg papers".** Asked for
  2026-09-03. A program or a method accumulates a cluster of papers:
  the one that introduced it, the follow-ups, the ones that describe
  a component, the ones that apply it. You want to see them
  together, and you know which they are; the app does not.

  **What is already there, and why it is not quite this:**
    - **Tags** (`sidecar["tags"]`, editable in the metadata dialog,
      free text) already group papers by a label the user chooses.
      For a set the user can enumerate, `acedrg` as a tag is most of
      the feature — so the first question is whether this is really
      a request for *better tagging* (a tag picker rather than a
      comma-separated entry box, tag chips on cards, click a tag to
      filter) rather than a new concept.
    - **Marks** are four colours with library-wide labels
      (`marks_config`), so one paper carries one; they cannot express
      several overlapping groups.
    - **Typed relations already exist but are narrow**: `si_of`
      links a supplement to its parent, `published_version` links a
      preprint to the published paper. Both are *pairwise* and
      automatic, not user-declared sets.

  **Three readings, and they want different things:**
    1. **A named set the user curates** — "Acedrg" holds these six
       papers. Closest to tags; cheapest by far. Wants tag chips on
       cards and click-to-filter to actually feel like grouping.
    2. **A pairwise "related to" link** between two entries, like
       `si_of` but symmetric and free-form. Better when the relation
       has a direction or a reason ("this supersedes that"), worse
       for "show me all of them".
    3. **Automatic clustering** — infer the set from citation
       overlap, shared title terms, or that they cite each other.
       The most interesting and the least predictable; it also
       cannot know that a paper *is* an Acedrg paper rather than
       merely adjacent to one.

  Worth asking which of these was meant before building anything:
  (1) is an afternoon and may be all that is wanted, (3) is a
  research project. Suggest (1) first — if a tag chip on the card
  and a click-to-filter make "show me the Acedrg papers" a
  one-click operation, the rest may never be missed.

  Note for whichever way it goes: tags are per-sidecar and so travel
  with the paper, which is the right property here — a curated set
  should survive re-import and be visible to the MCP server.


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
- **Show the author's avatar in the main-window author popover.**
  Clicking the author list on a card opens the popover built by
  `_open_authors_popover` / `_build_author_row` (`browse.py:5066`,
  `:5143`). When the author has a photo, show it there — small, next
  to the name.

  **The avatar is a curation signal, not decoration** — this is the
  point of the feature and it drives the design below. An author only
  has a photo because the user went and found one, so its presence
  means "I care about this person". In a fifty-name author list, that
  makes it a **scanning aid**: a face jumps out where a name would be
  missed. Everything else follows from that job.

  (Secondary, but real: avatars currently appear *only* in the
  Authors window, so the effort of setting one is invisible from the
  main window where most time is spent.)

  **The lookup is already deterministic and needs no network or DB
  column:** `author_image.image_path(authorship)` returns
  `<root>/.author-images/<key>.png` (key = OpenAlex ID, else ORCID)
  or `None` when the authorship carries neither. Then an
  `os.path.isfile`. Scraped-only names have no identifier, so they
  can never have one — consistent with their existing greyed-italic
  treatment.

  **Layout.** Each author currently occupies *two* grid rows — name
  button on the first, institution on the second — so the avatar
  wants a new leading column spanning both, and every existing
  `grid.attach` column index shifts by one. Small, but it is the
  actual work.

  **Show blank space, not initials, when there is no photo** — and
  given the rationale above this is not a preference but a
  requirement. `Adw.Avatar` would give an initials disc for free (as
  on the author page), but if every author gets a disc the signal is
  destroyed: the whole value is that *most rows have nothing there*.
  Sparseness is the feature. Reserve the column width so names stay
  aligned, and draw nothing. Put the column at the leading edge, so
  the eye can run down one column and stop at the faces.

  **Do not reorder** to bring photographed authors to the top —
  author order carries meaning (first/last position is already shown)
  and scanning works fine with the images in place.

  **Resist widening the signal.** The `author_trail` table is a
  second, cheaper record of "authors I have looked at", and it is
  tempting to mark those rows too. That would dilute it: trail
  membership accrues from a single click, whereas a photo takes
  deliberate effort, and it is precisely the *cost* of the avatar
  that makes it worth scanning for. If a broader "authors of
  interest" mark is ever wanted, it should be its own explicit flag,
  not an inference from either.

  **Watch the cost on long author lists.** The popover is rebuilt on
  each open and some papers carry 50+ authors. The files are <=512px
  PNGs and scaling to ~20px is cheap, but decoding dozens per open is
  not free — cache the scaled `Gdk.Texture` by trail key rather than
  re-decoding, since the same authors recur across cards.

- **Shared papers between an author and a collaborator.** On the
  author page, act on a collaborator chip to see the papers the two
  have written *together*.

  **Right-click, and left-click keeps its current job.** The chips are
  already `Gtk.Button`s whose click calls `_open_coauthor` and
  navigates to that person's page — a good, discoverable primary
  action that should not move. So the new action wants a secondary
  gesture, and a context menu is the conventional one.

  Note this is deliberately *not* the avatar's "either mouse button
  opens the menu" rule (7b1c940). That was right there because the
  avatar has no primary action — its menu *is* the action, and users
  were right-clicking it because it reads as an object. A chip does
  have a primary action, so the two buttons must differ. Worth
  recording so the inconsistency looks considered rather than
  accidental.

  **Discoverability is the weak point**, and the avatar commit
  already learned this lesson — it added a tooltip and pointer cursor
  "so the click is discoverable before the first guess". Same applies:
  the chip tooltip should say so outright, e.g. *"Open Kevin Cowtan —
  right-click for shared papers"*. On a Mac laptop right-click is a
  two-finger tap, so the hint matters more than it would on Linux.

  **Two sources, and the local one should come first.** Papers in
  *this library* that both authors appear on can be found from the
  DB — instant, offline, and usually the more interesting question
  ("which of my papers do these two share?"). OpenAlex then gives the
  complete set. Same in-library-then-discovered split the app already
  uses elsewhere.

  **OpenAlex query — the obvious form is wrong.** Intersecting two
  authors is:

    ```
    filter=authorships.author.id:A5071138931,authorships.author.id:A5018808577
    ```

  Repeat the whole key, comma-separated. Do **not** use the `+`
  AND-syntax (`authorships.author.id:A+B`): it silently fails and
  returns the *first* author's works alone. Measured 2026-09-01 —
  Emsley+Cowtan returned 79 works via `+`, which is exactly Emsley's
  solo total, whereas the comma form returned 11, all verified to
  carry both authors. A `+` query therefore looks plausible and is
  badly wrong, so a test should pin it.


- **"Find metadata" mis-parses a pasted author list.** Reported
  2026-09-03 on `experiences-protein-ligand-studies:Pearce2022` in
  the moorhen catalogue. Pasting the authors as they appear on the
  card —

      Nicholas M Pearce · Rachael Skyner · Tobias Krojer

  returned *Hierarchical Linear Models*, *The Economics of Climate
  Change* and Metropolis's *Equation of State Calculations*. Traced:

      parse_citation_hint("Nicholas M Pearce · Rachael Skyner · …")
        -> {'surname': 'Nicholas', 'year': None,
            'journal': 'M Pearce · Rachael Skyner · Tobias Krojer'}

  Three separate faults, each of which alone would have been
  survivable:

    1. **`·` is not a recognised separator.** `parse_citation_hint`
       splits at "et al.", else at the first comma, else after the
       first *whitespace-delimited token*. With middle dots and no
       comma, the whole line after "Nicholas" became the journal.
       The middle dot is what the app's own author display uses, so
       it is the single most likely thing a user pastes.
    2. **Display order is assumed to be citation order.** The parser
       takes the first non-initial token as the surname, which is
       right for "Pearce, N." and wrong for "Nicholas M Pearce". A
       pasted author list is almost always given-name-first.
    3. **Ranking by citations turns a bad match into a confident
       one.** Searching a common given name with no year and no
       journal returns the most-cited papers by anyone called
       Nicholas, and the UI then reports "top result 2× more cited
       than next" — reading as confidence about a result that is
       pure noise.

  **The dialog already knew the answer.** Title, Year (2022) and
  Journal (Frontiers in Molecular Biosciences) were all filled in on
  screen while the search ran on the word "Nicholas" alone. The
  strongest fix is not a better parser: it is to search from the
  fields already present, with the typed fragment as an *optional*
  extra. A title search alone would have found this paper first.

  **The shape the user wants (2026-09-04): a "Search using current
  metadata" button.** Not a cleverer parser — a second button beside
  Search that skips parsing entirely, because the dialog's fields
  are *already* partitioned by type. Title is Title, Year is Year,
  the authors are one per line with the surname as the last token.
  The typed box stays for the case it was built for: a citation read
  off a printed page, where there are no fields to draw on.

  Note the actual goal behind the report: **getting the DOI, in
  order to find out whether a PDF can be fetched.** So the result of
  a successful match should make that next step obvious — filling
  the DOI field is necessary but not sufficient.

  **One gap to close first:** `find_citation_candidates(surname,
  year, journal, max_n)` has no title parameter, and the title is by
  far the strongest signal here — "Experiences from developing
  software for large X-ray crystallography-driven protein-ligand
  studies" identifies the paper on its own. Wants an OpenAlex
  `title.search:` query, preferred when a title is present, with the
  existing surname/year/journal ladder as the fallback.

  Suggested order:
    - The "Search using current metadata" button, with a title
      query, and say what was searched in the hint line.
    - Accept `·`, `;`, `&`, ` and ` and newlines as author
      separators, and treat a multi-name list as "surname = last
      token of the first name".
    - When only a surname was extracted and it is also a common
      given name, say what was searched for rather than silently
      ranking by citations — the hint line already has the room
      ("10 candidates (year ignored)").

- **Filter the author trail by recency.** Asked for 2026-09-01: the
  trail has grown to the point where it needs arranging. Three
  buttons along the bottom of the sidebar — **This Week**, **This
  Month**, **All** — behaving as radio buttons.

  Everything needed is already there. `author_trail.last_viewed` is
  stamped by `index.touch_author_trail` on every selection, so the
  filter is a `WHERE last_viewed >= ?` on top of
  `index.list_author_trail` (`index.py:837`), which today is an
  unconditional `SELECT * ... ORDER BY position`. Give it an
  optional `since` argument rather than filtering in the window, so
  a long trail does not build rows it then hides.

  Put them in the sidebar's `Adw.ToolbarView` as a bottom bar
  (`author_works.py:1908` already adds a top bar to it), so they sit
  below the scrolled list and stay put while it scrolls.
  **`Adw.ToggleGroup`** is the widget — it exists in libadwaita
  1.9.3, which is what we build against — and gives the segmented
  look without hand-linking three `Gtk.ToggleButton`s.

  Three things to get right:
    - **Don't hide the author currently being read.** Selecting
      someone, then narrowing to This Week, must not make the
      selected row vanish from under the open page. Either always
      include the selected key, or keep the selection and let the
      row reappear.
    - **Remember the choice**, as the collapsible sections now do —
      `prefs.get_section_expanded` / `set_section_expanded` show
      the shape; this wants its own key rather than reusing that
      one.
    - **Say when a filter is why the list is short.** "No authors
      viewed this week" beats an empty sidebar that looks broken —
      the window already has empty-state handling to follow
      (`author_works.py:1919`).

  Worth considering while in here: `last_viewed` is nullable, and
  rows added before it existed have NULL. Decide whether those sort
  as ancient (drop out of This Month) or fall back to `added_at`.
  Falling back is kinder and the column is right there.

- **Don't search until three characters are typed.** Reported
  2026-09-01: typing in the search bar feels laggy — the second and
  third characters take a noticeable while to appear.

  `browse._on_search` (`browse.py:2491`) is
  `self._reload(entry.get_text() or None)`, wired straight to
  `search-changed`, so every keystroke runs a full `index.search`
  and rebuilds the card list. The cost is worst exactly where it is
  least useful: a one-character query matches almost the whole
  library and returns the full `limit=500`, so the first keystroke
  triggers the most expensive query and the largest rebuild of the
  whole sequence, and the second and third keystrokes queue behind
  it.

  Two remedies, and both are wanted:
    - **A minimum query length of 3.** Below it, show the unfiltered
      list. On its own this only moves the problem — from the 3rd
      character on, every keystroke still reloads.
    - **Debounce the search path.** `reload_policy.reload_delay_ms`
      and `_do_debounced_reload` (`browse.py:4261`) already exist
      for watcher-driven reloads; the search path does not use
      them. Typing is burstier than a filesystem event, so it may
      want a shorter debounce of its own (~150 ms) rather than the
      300 ms default.

  Watch when implementing: deleting back below three characters must
  restore the full list rather than leave the last filtered view on
  screen, and `_on_filter_chip` / `browse.py:5202`
  (`self.search.set_text(query)`, which relies on `search-changed`
  firing) must still apply its query immediately — a programmatic
  set is not a keystroke and should not wait for a debounce or trip
  the minimum length.

  The same entry-per-keystroke shape is in the Subscriptions search
  and the Authors filter; check those while in here.

- **Right-click the JATS chip → "Summarise with <agent>".** Noted
  2026-08-31: right-clicking the JATS chip pops the *"Cite this
  paper as…"* menu, because the chip has no gesture of its own and
  the event bubbles to the card's right-click handler
  (`browse.py`, `cite_click`). Sensible menu, startling place to
  meet it. Each chip that means something specific should carry its
  own context menu — the JATS chip's obvious one being **generate a
  summary from this stored full text**.

  **Why this is a nicer route than the MCP summaries design.** That
  entry (under "Prompted by Paperlib") is blocked on MCP
  *sampling*: the server cannot ask the client for a completion, so
  Alexandria can never initiate a summary and the flow has to start
  in Claude. But a **configured agent command** sidesteps it
  entirely: a Preferences entry holding something like
  `claude -p` (or any command reading a prompt on stdin and
  writing prose on stdout), invoked as a subprocess with the JATS
  text piped in. The model still lives outside the app — no API
  keys, no model dependency, offline-first intact — and the user
  chooses the agent. On return, write it via the same path
  `set_summary` uses so provenance is recorded identically
  (`model` = the configured command, `source` = "jats").

  Details worth settling when building:
    - Run it off the main thread with a timeout and a visible
      "summarising…" state; a local model can take minutes.
    - Offer it only where there is something to summarise — the
      chip is already the "we hold the full text" signal.
    - Same guard as the MCP tool: never overwrite a hand-written
      summary.
    - The PDF-text tier gives the same affordance for papers with
      no JATS; the chip is just the most natural first home.

- **The Edit-metadata dialog should not be modal.** Reported
  2026-08-30: with a PDF open in the viewer, opening Edit metadata
  froze the viewer — no zoom, no scrolling — so the obvious check
  ("does the DOI on the page match the DOI in the metadata?")
  can't be done while the dialog is up. `edit_dialog.open_editor`
  creates its window with `modal=True` (`edit_dialog.py:91`),
  which grabs input across the application, viewer included.
  Consulting the paper while correcting its metadata is exactly
  the workflow the dialog exists to serve.

  Fix is one flag, but two things want thought first:
    - **Concurrent sidecar writes.** The viewer saves highlights
      and comments to the same sidecar the dialog saves metadata
      to; with both open, last-write-wins could drop one side's
      changes. Either re-read-and-merge before writing (the
      cross-host race the Multi-host section describes, now
      in-process), or have the dialog write only the fields it
      owns.
    - **Several editors at once.** Non-modal means N dialogs for
      N papers. Fine, but the window title must name the paper
      (it does) and a second editor on the *same* paper should
      raise the first rather than open a duplicate.

- **"Rename to metadata" button on the paper card.** Rename the
  PDF file so the filename reflects (to some extent) the title,
  author and year — publisher downloads arrive as opaque names
  (`s41586-024-07487-w.pdf`, `acs.jcim.4c02293.pdf`) and the
  library folder is unreadable outside the app. Another button in
  the card's action row (alongside Open / View / Edit / Rename /
  Delete); proposes something like
  `Smith-2024-Semantic-community-model.pdf` (first-author surname,
  year, first few title words, sanitised and length-capped), shown
  for confirmation/edit before applying. More than an `os.rename`:
  the sidecar (`*.pdf.alexandria`) and thumbnail must move with
  the PDF, the sidecar's `pdf_filename` basename and the DB row
  need updating, and the watcher will see the rename as a
  delete + create — reuse the existing manual-Rename plumbing,
  which already deals with all of this; the new part is only
  generating the proposed name from the metadata.

- **Author photo on the author page.** Show a small blank-silhouette
  placeholder next to the author's name (Authors window /
  `AuthorPage` header); clicking it lets the user either pick an
  image file from disk or fetch one automatically. Best automatic
  source is probably **Wikidata**, not a raw web search: many
  researchers with ORCIDs have Wikidata items (lookup by ORCID via
  SPARQL or the wbsearch API), and the item's P18 property is a
  vetted portrait from Commons — no is-this-a-person classification
  needed. If a web-search fallback is still wanted, face detection
  to filter results is easy locally: `opencv-python`'s Haar-cascade
  face detector (no extra model downloads) answers "does this image
  contain exactly one reasonably-sized face"; `mediapipe` or
  dlib-based `face_recognition` are heavier but more accurate.
  Storage: cache the image under the library root keyed by
  OpenAlex ID / ORCID; show the silhouette when absent.

- **Click-through on award IDs in the card "Funded by" line.** The
  award_id is shown today only in the tooltip; it would be far
  more useful if clicking it opened (somehow) the funded
  application's abstract or summary. Gateway to Research
  (`gtr.ukri.org` — the UKRI public-facing grant database) is the
  most likely source for UK funders (UKRI/EPSRC/BBSRC/MRC etc.):
  it exposes a REST API that takes a grant reference and returns
  the project record. NIH RePORTER is the obvious analogue for US
  funders. Per-funder routing: dispatch on the `funder` name to
  pick the right registry (`funding_links.funding_url` already does
  this for GtR / NIH / NSF / OSTI / CORDIS).

  Unmatched funders currently fall through to **plain text** — the
  earlier DuckDuckGo-search fallback was removed (it landed on
  useless result pages). If a generic fallback is wanted again,
  it needs to be one that actually resolves to the grant record,
  not a bare web search.

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

## Structured full text (JATS) instead of PDF archaeology

Everything the citation-following work does is reconstruction of
structure the publisher *had* and threw away. Worth writing down while
the evidence is fresh, because it argues for a different source rather
than more heuristics.

- **JATS v3: find the reference *entries* on the page too.**
  Path E (2026-08-31) uses the JATS to find where the citations are;
  the complement is using it to find where the reference list is.
  Measured over the 71 papers that have JATS: Path E was reached by
  7 of them (the other 64 already had links from paths A–D) and
  fixed 1. The remaining 6 all fail for the same reason —
  `parse_bibliography` finds *nothing* in the PDF, so there is no
  target to jump to and Path E cannot help. Anchoring each
  `parse_ref_list` entry's text on the page would supply those
  positions. A quick probe says it depends on the publisher: Protein
  Science `pro.70321.pdf` anchors 74 of 79 entries on the first 48
  characters, but RSC (`c8sc05372c.pdf`) prints "M. H. S. Segler"
  where the XML has "Segler M. H. S." — different word order, no
  common prefix — and Frontiers (`fmolb-09-861491.pdf`) runs the
  authors together without spaces. So it needs an
  order-insensitive anchor (surname + year + a distinctive title
  word, say) rather than a prefix match.

- **Path E limitation: collapsed ranges.** Where the XML enumerates
  `16,17,18,19,20` and the page prints "16–20", the endpoints link
  and the interior numbers do not — those digits are not on the
  page. On the Casañal paper that is 4 of the 8 remaining misses
  (56 of 64 citations link). Clicking "16–20" goes to reference 16,
  which is the reasonable outcome; recording it so it is not
  rediscovered as a bug.

**What we actually built to make `[7]` clickable.** Three fallback
paths: publisher-specific destination names (Springer `CR<N>`, OUP
`-B<N>`, an InDesign export embedding the whole reference string behind
a variable number of BOMs); then geometry, matching a destination's
y-coordinate to a parsed bibliography entry within 12pt; then regex
over page text when a PDF carries no internal links at all. Alongside
that: a "length-align `get_text` to `get_text_layout`" dance repeated
in three functions because producers disagree about newlines;
de-hyphenation by guesswork; reading order inferred from detected
column edges; `parse_bibliography` deciding by inspection whether a
reference list is numbered or author-year, and still mistaking a
manual's numbered introduction for one (hence
`_looks_like_reference_list`). Front matter bleeds into the first
body sentence because a PDF has no notion of blocks — only that these
glyphs are near those glyphs.

**Root cause.** PDF is a page description language: instructions for
putting marks on paper. It was never meant to carry document
structure. Scholarly articles are typeset *from* JATS XML, in which
the reference list is marked-up elements and every citation is a real
cross-reference — and that is discarded in the last step, leaving
every downstream tool to guess it back.

**The alternative: fetch the structured version where it exists.**

- **Europe PMC** serves JATS over
  `https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML`,
  no registration or API key, ~6.5M open-access articles. Keyed by
  PMCID, so it needs a DOI to PMCID resolution step first.
- **arXiv** exposes e-print LaTeX source, which carries `\cite` keys
  and a real bibliography.
- **bioRxiv / medRxiv** full text — worth checking what their TDM
  channels actually expose before counting on it.

**What it would buy.** Exact reference lists with no parsing;
citations as real cross-references with no `ref_n` recovery; sentence
and section boundaries that are marked up rather than inferred, which
would make the "Cited here as:" context exact instead of heuristic;
and no front-matter bleed, because the affiliation block is tagged as
one. Beyond the citation work: section-aware search ("only Methods"),
proper abstracts (abstract-display item), tables as data and MathML
instead of mangled glyphs; feeds FTS, the static HTML index (a
reflowable reading view, not just a PDF link), and makes the MCP
server's `get_pdf_texts` dramatically better for ask-the-library —
Claude reading JATS beats pdftotext output every time. And later,
citation-context features: "how does this paper cite X — in passing,
in Methods, critically?"

**On-disk shape.** Extend the same-basename convention:
`paper.pdf` + `paper.pdf.alexandria` + `paper.pdf.jats.xml` travel
together; teach the watcher/importer to ignore the new extension;
sharing and Drive-sync inherit it for free. Stdlib `xml.etree`
parses JATS fine — no new dependencies; the fetch helper fits the
existing `metrics.py` pattern.

**What it does not replace.** The PDF stays the thing the user reads
and annotates — sidecar highlights are anchored to PDF coordinates,
and that doesn't change. This is a *structure and metadata* source
running alongside, not a second viewer.

**Honest caveat on coverage**, which matters more now the audience is
scientists in general rather than one structural biologist: full-text
JATS is essentially an OA phenomenon — PMC / Europe PMC (includes
some Acta Cryst), PLOS, eLife, bioRxiv/medRxiv, MDPI, Frontiers;
paywalled Elsevier/Wiley/ACS won't have it. Europe PMC
is heavily biomedical and its `fullTextXML` is PMC-keyed, so preprints
(PPR), patents and Agricola records are searchable for metadata but
have no full text there. Physics, chemistry, engineering and astronomy
are thinly served or absent. So this improves some fields a great deal
and others not at all — it never removes the need for the PDF paths,
it just means the best-covered papers stop needing them.

**Sequencing.** Cheapest useful first step is not a rewrite:
  - **v0 — fetch and store.** At import time, resolve DOI → PMCID,
    fetch the JATS when available, store it beside the PDF (plus a
    backfill pass over the existing library). Data on disk before
    any consumer exists.
  - **v1 — references.** Use the JATS *only* to supply the reference
    list and citation targets — feeding the same
    `{page: [(rect, target_page, target_top, ref_n)]}` shape the
    viewer already consumes, or superseding `parse_bibliography` for
    those papers. The rect side still has to come from the PDF,
    since that is where the user is clicking.
    *(Shipped 2026-08-31: `jats.parse_ref_list` supersedes
    `parse_bibliography` where the JATS exists, and `parse_xrefs` +
    `references_pdf.find_jats_citations` supply the rects — the
    viewer's Path E.)*
  - **v2 — full text.** Section-aware FTS and citation contexts.

  *(v0 shipped 2026-08-28: `alexandria/jats.py`, import-time hook,
  "Fetch JATS full text…" backfill in the hamburger menu, sidecar
  `jats` block with 30-day re-check of absent answers.)*

**Follow-on: JATS → Markdown converter** (2026-08-28), so LLM
tooling (the MCP server's text tools, ask-the-library) can feed on
Markdown rather than raw XML — Claude reads Markdown best of all.
Open question before building: where the generated `.md` lives.
It is a *derived* artefact (regenerable from the `.jats.xml` at any
time), which argues against a third same-basename sibling cluttering
the library dir — candidates: a cache dir under the library root,
generate-on-demand in the MCP server with no file at all, or accept
the sibling for greppability. Needs a short design chat.

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

### Research

  - What are ResearchRabbit, Connected Papers, Scite, Litmaps
    - can they help us?
  - What are Consensus, SciSpace, and Elicit?
  - What are the tools here: https://effortlessacademic.com/tools/
    How do we compare?

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

## Prompted by Paperlib (surveyed 2026-08-26)

Paperlib (`Future-Scholars/paperlib`, GPL-3.0, Electron/TS, ~2.3k
stars) is the closest direct peer found so far — same bet on metadata
scraping, plus PDF fetch, RSS feeds, tags/folders/notes and cloud
sync. Full survey in `chat-stuff/competitors-paperlib.md`. It has
**no PDF annotation and no author-centric features at all**, which
makes our viewer (highlights, comments, citation-following with
jump-back) and the Authors side the durable differentiators. Two
things it does better:

- **Widen the metadata scrapers.** Their source list is materially
  wider than our OpenAlex + Crossref + Unpaywall + PDBe: arXiv,
  Semantic Scholar, Google Scholar, Springer, Scopus, openreview,
  IEEE, DBLP, Papers with Code, NASA ADS, SPIE, ChemRxiv,
  bioRxiv/medRxiv. Now that the audience is scientists in general
  rather than this library's owner, breadth is the point rather than
  scope creep, and a source being outside structural biology is not
  an argument against it. Ranking by what each actually adds:

    - **Semantic Scholar first.** The only true general aggregator in
      the list — an OpenAlex peer from AI2, 200M+ papers across all
      disciplines, with a free API (no key needed for light use; a
      free key buys a dedicated ~1 req/s). Independent coverage where
      OpenAlex is thin, plus TLDR summaries, citation-influence
      scores and citation *contexts* — the sentence in which one
      paper cites another, which the viewer's citation-following work
      could surface directly. Shaped like the existing OpenAlex
      client, so it slots in beside it.
    - **Preprint servers next** — bioRxiv/medRxiv, ChemRxiv, and
      arXiv for the physical sciences. OpenAlex is often thin or late
      on preprints, the same freshness gap
      `feed._fetch_landing_pdf_url` already works around.
    - **Subject archives are for their own audiences.** NASA ADS
      (astronomy/astrophysics/planetary/heliophysics, 16M+ records,
      free API but needs an account token) is the definitive index
      for astronomers and worthless to anyone else; it has also been
      funded to extend into earth science and NASA-funded biological
      and physical sciences, so its scope may broaden. Only worth
      doing once there are astronomers using this.
    - **SPIE is a publisher, not an aggregator** — its Digital
      Library is SPIE's own 650k-item catalogue of optics and
      photonics journals and conference proceedings. No public API
      found, so Paperlib is likely scraping it: exactly the
      per-publisher maintenance cost flagged in the
      publisher-page-harvest entry. Lowest priority of the four.

- **Citation insertion into the document being written — both
  competitors have it, we do not.** Raised by Paperlib (MS Word
  plugin plus a Spotlight-like paste popup); **confirmed against
  JabRef 2026-09-02**, which has native BibTeX/BibLaTeX with
  LibreOffice and Word integration, thousands of built-in citation
  styles, and spent GSoC 2026 effort *improving* citation insertion
  in LibreOffice. Two independent projects having it and us not is a
  signal rather than a coincidence — **promoted from "consider" on
  that evidence.**

  We have BibTeX / RIS / CSL *export* but no "drop a citation into
  the document I am in right now" path. The cheap version is not the
  plugin: a **global shortcut that searches the library and puts a
  formatted citation on the clipboard** closes most of the gap, and
  the formatting already exists (`csl_format`, `bibtex_export`,
  `ris_export`) — a UI surface over solved problems, with no
  per-word-processor integration to maintain.

  Deliberately *not* matching them on the plugin: LibreOffice and
  Word integrations are a large, brittle, per-application surface,
  and JabRef has twenty years of it. The clipboard route gets the
  workflow without the maintenance.

- **LLM paper summaries, via MCP rather than in the app.** Paperlib
  advertises LLM summarisation, auto-tagging and semantic search. We
  can have the first of those without taking on any of the cost that
  put RAG in "Speculative / maybe-never": the objection there is
  adding an LLM dependency to a deliberately offline-first app, and
  going through `alexandria_mcp` dissolves it. The model lives in the
  MCP *client* (Claude Desktop / Claude Code), so we ship no model,
  no API-key handling and no heavy dependencies; the app still works
  fully offline and summaries are simply absent until a client is
  connected.

  **Protocol constraint that decides the shape — check before
  designing any UI around this.** MCP has a feature for exactly this
  job: *sampling* (`sampling/createMessage`), where a server asks the
  client for a completion, with the client owning model choice and
  API keys. It is **not available**: unsupported in Claude Desktop,
  and an open feature request for Claude Code
  (anthropics/claude-code#1785). So Alexandria cannot initiate a
  summary. The flow is Claude-initiated — the user asks, inside a
  client with our MCP server connected — which rules out a
  "Summarise" button that works on its own. Re-check this: if
  sampling ever lands, an in-app trigger becomes possible and the
  design below gains a second entry point without changing.

  **Flow.** User asks in the client → `recently_added` (exists) →
  `get_summary_input` (new) → model writes the summary →
  `set_summary` (new) → Alexandria displays it.

  **Two new tools.** The read side already exists: `get_pdf_texts`
  hands over sliced, truncated PDF text and `get_sidecars` exposes
  the sidecar that is the source of truth. What is missing is a
  **write** path — the server's only writing tool today is
  `add_paper_by_doi`.

    - `set_summary(paper_id, summary, model, source_tier)` writing
      into the sidecar beside the existing enrichment fields, so it
      survives re-indexing and travels with the library like every
      other piece of user data. Recording *which model* and *which
      tier* is not optional: a summary is a dated machine opinion,
      and the UI must not let it be mistaken for the abstract. As a
      write tool it wants the same care as `add_paper_by_doi` —
      validate the ID, cap the text length, no arbitrary sidecar
      writes.
    - `get_summary_input(paper_ids, max_chars=...)` returning
      `{paper_id, tier, text}` and choosing the best available
      source. Keeping the tier choice in Python rather than in
      prompt-space makes it testable and stops the model getting it
      subtly wrong.

  **Three-tier input, not a JATS gate.** JATS is better input — clean
  text, tagged sections so Methods and the reference list can be
  skipped, no page furniture (the Science extraction spliced
  "…science.org on August 21, 2026" into the middle of a sentence),
  MathML instead of mangled glyphs. But measured against this
  library, only **72 of 127 DOIs (57%) have full text in Europe
  PMC** — and that is a biomedical-heavy library, i.e. Europe PMC's
  best case and no longer the audience. Gating summaries on JATS
  would make the feature work for some users and silently not for
  others. So: **JATS → PDF text → abstract**, with the tier recorded.
  Quality upgrade on a feature that always works, not a
  precondition.

  **Sidecar shape:**

    ```json
    "summary": {
      "text": "...", "model": "claude-opus-5",
      "source": "jats", "generated_at": "2026-08-26T18:30:00Z"
    }
    ```

  **Sequencing — each slice independently useful.**

    1. Sidecar field + card / detail-view display. No MCP change;
       testable by hand-writing a summary into a sidecar. Proves the
       UI before any plumbing.
    2. `set_summary` — end-to-end from a connected client.
    3. `get_summary_input`, PDF tier only. Works for 100% of papers.
    4. JATS tier. Needs DOI to PMCID resolution; see the structured
       full text section.
    5. *(Optional)* a pending queue. If an in-app affordance is
       wanted despite the sampling gap, a button can only *mark*
       papers as wanting a summary, with a `pending_summaries()` tool
       letting the client collect them on next connect.

  **What not to do:** have Alexandria call the Anthropic SDK
  directly. It is only a few lines, and it reintroduces exactly what
  the MCP route exists to avoid — a model dependency, API-key
  handling, and an app that no longer works offline. The model
  belongs in the client.

  The same client-side pattern extends to auto-tagging and
  natural-language search later, still without new dependencies —
  which makes this worth designing as a general "let a connected
  client write derived fields back" path rather than a
  summaries-only special case.

Explicitly **not** chasing their cloud sync — offline-first is
deliberate; see Multi-host / NFS.

Caveat on their LLM claims: the summarise / auto-tag / semantic-search
features appear in marketing copy and a 2024 writeup but in none of
the introduction docs fetched 2026-08-26. Possibly newer,
extension-only, or aspirational. Worth re-checking before treating it
as a competitive fact — though the MCP route above is worth doing on
its own merits regardless of what they ship.

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

## Bug?

  Round-trip a .bib file - does it preserve the fields? I think
  page numbers may be missing. Needs a test.

  Why doesn't Charlotte Deane appear as the PI of Nicholas Pearce?
  Investigate.
  - Resolved: Not a bug. Frank von Delft is the last author of those
    papers.

  Opening the viewer's sidebar prints, about eleven times:
  `gtk_widget_size_allocate(): attempt to allocate AdwBin widget
  with width -2147482828 and height 922`.
  - Resolved: Not our bug, and harmless. Investigated 2026-08-31 on
    libadwaita 1.9.3. A `G_DEBUG=fatal-warnings` backtrace under gdb
    puts the bad width in libadwaita itself — frame 4 is
    `gtk_widget_allocate` emitting the warning, frame 5 is
    libadwaita (`AdwOverlaySplitView` allocating its own `AdwBin`
    child), frame 6 is `gtk_widget_allocate` again. Our code never
    allocates that widget. `-2147482828` is `G_MININT + 820`, the
    window width minus something near `G_MAXINT` — an internal
    underflow. It fires only during the sidebar's *open animation*:
    zero warnings with `gtk-enable-animations` off, or if the
    sidebar is shown before `present()`. Nothing renders wrongly.
    Ruled out, by substituting plain labels and stubs for each in
    turn: our sidebar content, our page content, our scroll
    handlers, the render worker.
  - **Do not re-investigate.** Left in place deliberately: a GLib
    log handler narrow enough to swallow this message would also
    mask real allocation bugs later. Worth filing upstream.

## alexandria-firefox-extension issues:

  https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/pro.4391
  fails to see the pdf.

