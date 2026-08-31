# Bumping the release version

Four files carry the version; all must agree. (The 0.2.0 release
missed the first one — `alexandria/__init__.py` still said `0.1.0`,
and that is the value the app displays via `browse.py`'s
`from . import __version__`, so the About/version readout was wrong.
Fix it as part of the next bump.)

1. `alexandria/__init__.py` — `__version__ = "X.Y.Z"`
   (the value the running app reports).
2. `pyproject.toml` — `version = "X.Y.Z"` (what pip/Flatpak build).
3. `data/io.github.pemsley.Alexandria.metainfo.xml` — add a new
   `<release version="X.Y.Z" date="YYYY-MM-DD">` entry (AppStream
   wants newest first; keep the old entries).
   **Check first whether the entry is already there.** The release
   notes are usually written before the bump, and writing them adds
   this entry too. In 0.4.0 this step was then applied a second
   time and landed on the *oldest* entry, relabelling 0.2.0 as a
   duplicate 0.4.0 and losing the first-public-release history
   (fixed in the commit after `0a05a70`).
   Verify with `appstreamcli validate data/*.metainfo.xml` before
   committing — it catches both a duplicate version and a
   mistyped tag, and should report only the pedantic
   `cid-contains-uppercase-letter` note.
4. `make-app.sh` — both macOS plist strings:
   `CFBundleVersion` and `CFBundleShortVersionString`.

Then:

- Commit as `Release X.Y.Z` (see 1e49a51 for the 0.2.0 shape).
- Tag it. Both `Release-0.2.0` and `v0.2.0` exist for 0.2.0 (the tag
  was re-pointed in July 2026); pick `vX.Y.Z` going forward — it is
  the convention forges and packaging tools expect.
- Push commits and the tag to both remotes:
  `git push origin main --tags` and `git push github main --tags`.
- If the Flathub manifest (separate repo, maintained with
  eunos-1128) pins a tag/commit, update it to the new tag.

Quick audit before tagging:

    grep -rn "X\.Y\.Zold" alexandria/__init__.py pyproject.toml make-app.sh

should return nothing. **Not `data/`** — the metainfo keeps an entry
for every past release, so the old version legitimately appears there
and including it makes the audit cry wolf every time (it would have
fired on the 0.3.0 entry during the 0.4.0 bump, and on 0.4.0 during
0.4.1). Audit that file with `appstreamcli validate` instead, as
step 3 says: it catches a duplicated version, a missing entry and a
mistyped tag, which grep cannot.
