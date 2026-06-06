# Alexandria — install / uninstall / release targets.
#
#   make install     — pip install --user, plus .desktop and icon
#   make uninstall   — reverse the above
#   make sync-icon   — copy data/<id>.svg → icons/hicolor/.../<id>.svg
#   make tar         — source tarball for the current commit
#
# Override PREFIX for system-wide install (needs root):
#   sudo make install PREFIX=/usr/local
#
# Override VERSION when tagging a release:
#   make tar VERSION=0.2.0
#
# Note on the two icon paths:
#   * data/$(ICON)               — canonical, installed by
#                                  `make install-data` (and any
#                                  Flatpak/distro build).
#   * icons/hicolor/scalable/apps/$(ICON)
#                                — duplicate consumed by GTK at
#                                  runtime via Gtk.IconTheme.
#                                  add_search_path("icons/")  in
#                                  alexandria/browse.py, so the
#                                  icon resolves when running from
#                                  source without an install step.
# Both must carry identical bytes. Edit `data/$(ICON)` and run
# `make sync-icon` (or it's done automatically by `make install`).

PREFIX  ?= $(HOME)/.local
VERSION ?= 0.1.0

DESKTOP_DIR := $(PREFIX)/share/applications
ICON_BASE   := $(PREFIX)/share/icons/hicolor
ICON_DIR    := $(ICON_BASE)/scalable/apps

APP_ID    := io.github.pemsley.Alexandria
DESKTOP   := $(APP_ID).desktop
ICON      := $(APP_ID).svg

# In-tree copy that GTK reads at runtime when running from source.
# Kept identical to data/$(ICON) via `make sync-icon`.
DEV_ICON_DIR := icons/hicolor/scalable/apps
DEV_ICON     := $(DEV_ICON_DIR)/$(ICON)

PYTHON ?= python3

.PHONY: install install-data uninstall uninstall-data clean dev tar sync-icon \
        app install-app uninstall-app

install: install-data sync-icon
	$(PYTHON) -m pip install --user .

install-data: sync-icon
	install -d $(DESKTOP_DIR) $(ICON_DIR)
	install -m 644 data/$(DESKTOP) $(DESKTOP_DIR)/
	install -m 644 data/$(ICON)    $(ICON_DIR)/
	-update-desktop-database $(DESKTOP_DIR) 2>/dev/null
	-gtk4-update-icon-cache  $(ICON_BASE) 2>/dev/null
	-gtk-update-icon-cache   $(ICON_BASE) 2>/dev/null

# Copy the canonical icon to the in-tree runtime location. Safe to
# re-run; cheap to make a no-op when the two are already identical.
sync-icon: $(DEV_ICON)

$(DEV_ICON): data/$(ICON)
	install -d $(DEV_ICON_DIR)
	cp -f $< $@

uninstall: uninstall-data
	-$(PYTHON) -m pip uninstall -y alexandria

uninstall-data:
	-rm -f $(DESKTOP_DIR)/$(DESKTOP)
	-rm -f $(ICON_DIR)/$(ICON)
	-update-desktop-database $(DESKTOP_DIR) 2>/dev/null
	-gtk4-update-icon-cache  $(ICON_BASE) 2>/dev/null

# Editable install for development.
dev:
	$(PYTHON) -m pip install --user -e .

# Source tarball for releases. `git archive` only includes tracked
# files and honours `.gitattributes export-ignore`, so the tarball
# stays clean automatically — no need to maintain a hand-rolled
# file list here. Default is HEAD; pass REF=<tag/branch/sha> to
# package a specific commit (e.g. `make tar REF=v0.1.0`).
REF ?= HEAD

tar:
	git archive --format=tar.gz \
	    --prefix=alexandria-$(VERSION)/ \
	    -o alexandria-$(VERSION).tar.gz \
	    $(REF)

# --- macOS .app bundle -------------------------------------------
# `make app` builds dist/Alexandria.app so the Dock shows the proper
# icon and app name instead of Python's. `make install-app` drops it
# into ~/Applications (no sudo). The .app launches `python3 -m
# alexandria.browse`, so `make install` (or `pip install --user .`)
# must have been run against a python3 the .app can find at launch.
APP_BUNDLE_NAME := Alexandria.app
APP_BUNDLE_DST  := $(HOME)/Applications

app:
	./make-app.sh

install-app: app
	install -d $(APP_BUNDLE_DST)
	rm -rf $(APP_BUNDLE_DST)/$(APP_BUNDLE_NAME)
	cp -R dist/$(APP_BUNDLE_NAME) $(APP_BUNDLE_DST)/
	@echo "installed $(APP_BUNDLE_DST)/$(APP_BUNDLE_NAME)"

uninstall-app:
	rm -rf $(APP_BUNDLE_DST)/$(APP_BUNDLE_NAME)

clean:
	rm -rf build dist *.egg-info alexandria*.tar.gz
	find . -name __pycache__ -prune -exec rm -rf {} +
