# pdforg — install / uninstall targets for desktop integration.
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

PYTHON ?= python3

.PHONY: install install-data uninstall uninstall-data clean dev

install: install-data
	$(PYTHON) -m pip install --user .

install-data:
	install -d $(DESKTOP_DIR) $(ICON_DIR)
	install -m 644 data/pdforg.desktop $(DESKTOP_DIR)/
	install -m 644 data/pdforg.svg     $(ICON_DIR)/
	-update-desktop-database $(DESKTOP_DIR) 2>/dev/null
	-gtk4-update-icon-cache  $(ICON_BASE) 2>/dev/null
	-gtk-update-icon-cache   $(ICON_BASE) 2>/dev/null

uninstall: uninstall-data
	-$(PYTHON) -m pip uninstall -y pdforg

uninstall-data:
	-rm -f $(DESKTOP_DIR)/pdforg.desktop
	-rm -f $(ICON_DIR)/pdforg.svg
	-update-desktop-database $(DESKTOP_DIR) 2>/dev/null
	-gtk4-update-icon-cache  $(ICON_BASE) 2>/dev/null

# Editable install for development.
dev:
	$(PYTHON) -m pip install --user -e .

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -prune -exec rm -rf {} +
