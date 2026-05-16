# Alexandria — install / uninstall targets for desktop integration.
#
#   make install     — pip install --user, plus .desktop and icon
#   make uninstall   — reverse the above
#
# Override PREFIX for system-wide install (needs root):
#   sudo make install PREFIX=/usr/local

PREFIX ?= $(HOME)/.local

DESKTOP_DIR := $(PREFIX)/share/applications
ICON_BASE   := $(PREFIX)/share/icons/hicolor
ICON_DIR    := $(ICON_BASE)/scalable/apps

APP_ID    := io.github.pemsley.Alexandria
DESKTOP   := $(APP_ID).desktop
ICON      := $(APP_ID).svg

PYTHON ?= python3

.PHONY: install install-data uninstall uninstall-data clean dev

install: install-data
	$(PYTHON) -m pip install --user .

install-data:
	install -d $(DESKTOP_DIR) $(ICON_DIR)
	install -m 644 data/$(DESKTOP) $(DESKTOP_DIR)/
	install -m 644 data/$(ICON)    $(ICON_DIR)/
	-update-desktop-database $(DESKTOP_DIR) 2>/dev/null
	-gtk4-update-icon-cache  $(ICON_BASE) 2>/dev/null
	-gtk-update-icon-cache   $(ICON_BASE) 2>/dev/null

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

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -prune -exec rm -rf {} +
