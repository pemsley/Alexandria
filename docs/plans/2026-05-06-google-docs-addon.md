# Google Docs add-on — citation tool backed by Alexandria server

A side panel in Docs with a search box; typing queries the Alexandria
sync server, results render as a list, click inserts a `[N]` citation
at the cursor. Tracks the cited list per-document and supports a
"Build bibliography" command.

## Architecture

```
Alexandria desktop ──git push/pull──▶ Server ◀──HTTPS──── Docs add-on
                                       │
                                  (same repo,
                                  read-only API
                                   on top of it)
```

The server has two faces over one storage backend:
- git-smart-HTTP for desktop sync.
- REST/JSON over the same sidecar tree for the Docs add-on.

API surface (small, read-only):

```
GET  /v1/papers?q=<text>&limit=20      → [{id, title, authors, year, doi, journal, bibtex_key, ...}]
GET  /v1/papers/<id>                   → full sidecar record
GET  /v1/papers/<id>/csl               → CSL JSON for that paper
GET  /v1/papers/<id>/bibtex            → BibTeX text
```

No PDFs, no comments, no highlights, no marks — citation metadata only.
That gives a clean privacy boundary.

Auth: per-user bearer token. Alexandria desktop generates it (Preferences
entry); user pastes into the add-on. Server maps token → user → repo.

## Prerequisites

- A Google account.
- The Alexandria server reachable at an HTTPS URL with a valid cert
  (Apps Script refuses HTTP and self-signed).
- The two endpoints above.

## Step 1: Create the project

Go to **script.google.com → New project**. Single editor with one file
`Code.gs`. Rename the project ("Alexandria Citations").

Apps Script projects have:
- `.gs` files — server-side JavaScript (V8 runtime).
- `.html` files — client-side, served via `HtmlService`.
- `appsscript.json` — manifest (visible via **File → Project Settings →
  "Show appsscript.json"**).

## Step 2: Manifest and scopes

Edit `appsscript.json`:

```json
{
  "timeZone": "Europe/London",
  "exceptionLogging": "STACKDRIVER",
  "runtimeVersion": "V8",
  "oauthScopes": [
    "https://www.googleapis.com/auth/documents.currentonly",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/script.container.ui"
  ]
}
```

Three scopes: edit current document only (least privilege), make outgoing
HTTPS calls (`UrlFetchApp`), show a sidebar UI.

## Step 3: The menu and sidebar

`Code.gs`:

```javascript
const API_BASE = "https://your.server.example/v1";

function onOpen() {
  DocumentApp.getUi()
    .createMenu("Alexandria")
    .addItem("Open citation panel", "showSidebar")
    .addItem("Build bibliography at cursor", "buildBibliography")
    .addItem("Set API token…", "promptForToken")
    .addToUi();
}

function showSidebar() {
  const html = HtmlService.createHtmlOutputFromFile("Sidebar")
    .setTitle("Alexandria");
  DocumentApp.getUi().showSidebar(html);
}
```

Reload the doc: a new "Alexandria" menu appears. (`onOpen` runs at
document open.)

## Step 4: Token storage

```javascript
function promptForToken() {
  const ui = DocumentApp.getUi();
  const r = ui.prompt("Paste your Alexandria API token:", ui.ButtonSet.OK_CANCEL);
  if (r.getSelectedButton() === ui.Button.OK) {
    PropertiesService.getUserProperties()
      .setProperty("alexandriaToken", r.getResponseText().trim());
  }
}

function getToken_() {
  return PropertiesService.getUserProperties().getProperty("alexandriaToken");
}
```

`UserProperties` is per-user, scoped to this script project, persistent
across sessions. The token never reaches the client side.

## Step 5: Server-side API call

```javascript
function searchPapers(query) {
  const token = getToken_();
  if (!token) throw new Error("No token set — run “Set API token…” first.");
  if (!query || query.length < 2) return [];
  const url = API_BASE + "/papers?q=" + encodeURIComponent(query) + "&limit=20";
  const resp = UrlFetchApp.fetch(url, {
    method: "get",
    headers: { "Authorization": "Bearer " + token },
    muteHttpExceptions: true
  });
  const code = resp.getResponseCode();
  if (code !== 200) throw new Error("Server " + code + ": " + resp.getContentText());
  return JSON.parse(resp.getContentText());   // returned to the sidebar via google.script.run
}

function insertCitation(paperId) {
  const props = PropertiesService.getDocumentProperties();
  let cited = JSON.parse(props.getProperty("cited") || "[]");
  let n = cited.indexOf(paperId);
  if (n === -1) { cited.push(paperId); n = cited.length - 1; }
  props.setProperty("cited", JSON.stringify(cited));

  const cursor = DocumentApp.getActiveDocument().getCursor();
  if (!cursor) throw new Error("Place the cursor in the document first.");
  cursor.insertText("[" + (n + 1) + "]");
}
```

`DocumentProperties` is per-document — citation list lives in *this doc*,
not the user. `cited` is the order in which papers were first cited.

## Step 6: The sidebar UI

`Sidebar.html` (a new HTML file in the project — **File → New → HTML
file**):

```html
<!DOCTYPE html>
<html>
<head>
  <base target="_top">
  <style>
    body { font: 13px/1.4 -apple-system, sans-serif; padding: 8px; }
    input { width: 100%; padding: 6px; box-sizing: border-box; }
    .hit { padding: 6px; border-bottom: 1px solid #eee; cursor: pointer; }
    .hit:hover { background: #f5f5f5; }
    .title { font-weight: 600; }
    .meta { color: #666; font-size: 11px; margin-top: 2px; }
  </style>
</head>
<body>
  <input id="q" type="search" placeholder="Search authors / title / DOI…" autofocus>
  <div id="hits"></div>

  <script>
    const q = document.getElementById("q");
    const hits = document.getElementById("hits");
    let timer = null;

    q.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(runSearch, 250);   // debounce keystrokes
    });

    function runSearch() {
      const query = q.value.trim();
      if (!query) { hits.innerHTML = ""; return; }
      google.script.run
        .withSuccessHandler(render)
        .withFailureHandler(err => hits.textContent = "Error: " + err.message)
        .searchPapers(query);
    }

    function render(rows) {
      hits.innerHTML = "";
      for (const r of rows) {
        const el = document.createElement("div");
        el.className = "hit";
        el.innerHTML =
          `<div class="title">${escape(r.title || "(untitled)")}</div>
           <div class="meta">${escape(authorList(r))} · ${r.year || ""} · ${escape(r.journal || "")}</div>`;
        el.onclick = () => insert(r.id);
        hits.appendChild(el);
      }
    }

    function insert(id) {
      google.script.run
        .withFailureHandler(err => alert("Insert failed: " + err.message))
        .insertCitation(id);
    }

    function authorList(r) {
      const a = r.authors || [];
      if (a.length === 0) return "";
      if (a.length <= 2) return a.join(", ");
      return a[0] + " et al.";
    }
    function escape(s) {
      return String(s).replace(/[&<>]/g, c =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    }
  </script>
</body>
</html>
```

The sidebar is a sandboxed iframe. `google.script.run` is the RPC bridge —
serialises arguments, calls the named server function, returns the result
via `withSuccessHandler`. Server-side functions can throw;
`withFailureHandler` catches.

## Step 7: Bibliography builder

Back in `Code.gs`:

```javascript
function buildBibliography() {
  const cited = JSON.parse(
    PropertiesService.getDocumentProperties().getProperty("cited") || "[]");
  if (!cited.length) {
    DocumentApp.getUi().alert("No citations yet.");
    return;
  }
  const token = getToken_();
  const cursor = DocumentApp.getActiveDocument().getCursor();
  if (!cursor) throw new Error("Place the cursor where the bibliography should go.");

  const lines = cited.map((id, i) => {
    const resp = UrlFetchApp.fetch(API_BASE + "/papers/" + id + "/bibtex", {
      headers: { "Authorization": "Bearer " + token },
      muteHttpExceptions: true
    });
    return "[" + (i + 1) + "] " + resp.getContentText();
  });
  cursor.insertText("\n\n" + lines.join("\n\n") + "\n");
}
```

Sufficient for numbered (Vancouver-ish) bibliographies. For author-year
or full CSL-rendered output, ship a `/papers/<id>/csl` endpoint and run
`citeproc-js` *inside the sidebar HTML* (server-side Apps Script doesn't
run npm packages cleanly).

## Step 8: Try it

1. **Save** the project.
2. Open any Google Doc.
3. **Extensions → Apps Script** once to confirm it's bound, or use the
   **Extensions** menu directly.
4. Reload the doc — the **Alexandria** menu should appear.
5. **Alexandria → Set API token…** → paste.
6. **Alexandria → Open citation panel** → search → click → `[1]` lands.

First run: Google asks for OAuth consent (the scopes from step 2). Says
"this app is unverified"; for personal use, **Advanced → continue
anyway**. Wider distribution requires the OAuth Application Verification
process (multi-day, separate concern).

## Distribution options

- **Personal / single user**: keep the script open in your account; copy
  or share to use in another doc.
- **Editor add-on, private to a workspace**: **Deploy → Test
  deployments / New deployment → Editor add-on**. Anyone in your
  workspace can install.
- **Workspace Marketplace (public)**: requires app review, OAuth
  verification, screenshots, support email, privacy policy. Real work.

## Limits

- `UrlFetchApp` daily quota: 20k calls for consumer accounts. Per-user.
- Each `UrlFetchApp.fetch` has a 60-second hard timeout.
- Sidebar HTML can use third-party JS via `<script src=...>` — including
  `citeproc-js` from a CDN — because the sandbox is a real browser
  context. Useful for CSL rendering.
- `google.script.run` arguments must be serialisable (no functions,
  Dates → ISO strings).
- `DocumentProperties` total ~9 KB. Store IDs only; re-fetch metadata.

## How this fits the bigger plan

- `/papers?q=` and `/papers/<id>/bibtex` are the only contract. Same
  endpoints can power a Word add-in (similar architecture, different
  SDK), a CLI, or a browser bookmarklet later.
- The CSL endpoint, when added, unifies with the desktop "Cite this
  paper as…" item in BACKLOG: same JSON, same `citeproc-js`, different
  chrome.
- Per-user bearer token is the simplest auth model. Marketplace add-ons
  would swap to OAuth-with-your-own-issuer or Google Identity Services —
  only worth it once the user count is non-trivial.

The hardest part is the server side (auth, repo→records), not the add-on.
