#!/bin/sh
# User-level install of Alexandria's desktop integration:
#
#   * SVG icon → ~/.local/share/icons/hicolor/scalable/apps/
#   * .desktop launcher → ~/.local/share/applications/
#   * refresh icon + desktop caches so taskbars / docks pick it up
#
# Run once after cloning / pulling a new icon. No root needed.
# To undo: remove the two files and re-run the two `update-*` cache
# commands.

set -eu

APP_ID="io.github.pemsley.Alexandria"
REPO=$(cd "$(dirname "$0")" && pwd)

ICON_SRC="${REPO}/icons/hicolor/scalable/apps/${APP_ID}.svg"
ICON_DST_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
ICON_DST="${ICON_DST_DIR}/${APP_ID}.svg"

DESKTOP_DST_DIR="${HOME}/.local/share/applications"
DESKTOP_DST="${DESKTOP_DST_DIR}/${APP_ID}.desktop"

# Pick the Python that runs the app. Override with ALEXANDRIA_PYTHON
# when your usual interpreter isn't on PATH (e.g. a custom build).
PYTHON_BIN="${ALEXANDRIA_PYTHON:-$(command -v python3 || true)}"
if [ -z "${PYTHON_BIN}" ]; then
    echo "error: no python3 on PATH; set ALEXANDRIA_PYTHON=/path/to/python3" >&2
    exit 1
fi

LAUNCHER="${REPO}/alexandria-browse.py"
if [ ! -f "${LAUNCHER}" ]; then
    echo "error: ${LAUNCHER} not found — is this the repo root?" >&2
    exit 1
fi
if [ ! -f "${ICON_SRC}" ]; then
    echo "error: ${ICON_SRC} not found" >&2
    exit 1
fi

# --- icon ---------------------------------------------------------
mkdir -p "${ICON_DST_DIR}"
cp -f "${ICON_SRC}" "${ICON_DST}"
echo "installed icon: ${ICON_DST}"

# --- .desktop -----------------------------------------------------
mkdir -p "${DESKTOP_DST_DIR}"
cat > "${DESKTOP_DST}" <<EOF
[Desktop Entry]
Type=Application
Name=Alexandria
GenericName=PDF Library
Comment=Personal library of scientific papers
Exec=${PYTHON_BIN} ${LAUNCHER} %F
Icon=${APP_ID}
Terminal=false
Categories=Office;Science;Education;
Keywords=pdf;bibliography;research;openalex;crossref;
MimeType=application/pdf;
StartupNotify=true
StartupWMClass=${APP_ID}
EOF
echo "installed launcher: ${DESKTOP_DST}"

# --- refresh caches ----------------------------------------------
# gtk-update-icon-cache is best-effort: many distros don't ship a
# per-user hicolor index and the missing-index warning is harmless.
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache --force --quiet \
        "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q "${DESKTOP_DST_DIR}" || true
fi
echo "caches refreshed"

cat <<EOF

Done. The Alexandria icon should now appear in your taskbar / dock
after the next launch. If it doesn't immediately:
  * log out / back in (some desktops cache the .desktop database
    until session restart),
  * or kill any running Alexandria window and relaunch from the
    application menu rather than the terminal.
EOF
