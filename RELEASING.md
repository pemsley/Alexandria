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
`grep -rn "X\.Y\.Zold" alexandria/__init__.py pyproject.toml data/ make-app.sh`
should return nothing.
