#!/bin/sh
# Build dist/Alexandria.app — a minimal macOS application bundle that
# launches Alexandria with its own Dock icon and identity instead of
# inheriting Python's.
#
# The bundle expects the alexandria package to be importable by the
# Python it picks at launch time (i.e. you have already run
# `make install` or `pip install --user .` against the same Python).
#
# Requires: make-icns.sh (which in turn needs rsvg-convert).

set -eu

APP_NAME="Alexandria"
APP_ID="io.github.pemsley.Alexandria"
REPO=$(cd "$(dirname "$0")" && pwd)

DIST="${REPO}/dist"
APP="${DIST}/${APP_NAME}.app"
CONTENTS="${APP}/Contents"
MACOS="${CONTENTS}/MacOS"
RES="${CONTENTS}/Resources"

ICNS_SRC="${REPO}/data/${APP_ID}.icns"

# Rebuild the .icns from the SVG so the bundle is never stale.
"${REPO}/make-icns.sh"

rm -rf "${APP}"
mkdir -p "${MACOS}" "${RES}"

cp "${ICNS_SRC}" "${RES}/${APP_NAME}.icns"

cat > "${CONTENTS}/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
                       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>             <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>      <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>       <string>${APP_ID}</string>
    <key>CFBundleVersion</key>          <string>0.0.1</string>
    <key>CFBundleShortVersionString</key><string>0.0.1</string>
    <key>CFBundlePackageType</key>      <string>APPL</string>
    <key>CFBundleSignature</key>        <string>????</string>
    <key>CFBundleExecutable</key>       <string>${APP_NAME}</string>
    <key>CFBundleIconFile</key>         <string>${APP_NAME}</string>
    <key>NSHighResolutionCapable</key>  <true/>
    <key>LSMinimumSystemVersion</key>   <string>11.0</string>
    <key>NSPrincipalClass</key>         <string>NSApplication</string>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key>       <string>PDF document</string>
            <key>CFBundleTypeRole</key>       <string>Viewer</string>
            <key>LSHandlerRank</key>          <string>Alternate</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>com.adobe.pdf</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
EOF

# Launcher. Tries $ALEXANDRIA_PYTHON first, then python3 from a
# fallback PATH (Finder-launched apps don't inherit the user shell's
# PATH, so we seed it with the usual Homebrew / user locations).
cat > "${MACOS}/${APP_NAME}" <<'EOF'
#!/bin/sh
set -eu

export PATH="/opt/homebrew/bin:/usr/local/bin:${HOME}/.local/bin:${HOME}/Python3.14/bin:/usr/bin:/bin:${PATH:-}"

# Homebrew GSettings schemas — Gtk.FileDialog aborts without these on
# macOS source launches. Harmless when the path doesn't exist.
export XDG_DATA_DIRS="/opt/homebrew/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

# Homebrew dylibs (PyGObject loads them at import time).
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_LIBRARY_PATH:-}"

# Quieten the libdispatch malloc warnings PyGObject triggers on macOS.
export MallocNanoZone=0

PY="${ALEXANDRIA_PYTHON:-$(command -v python3 || true)}"
if [ -z "${PY}" ]; then
    /usr/bin/osascript -e 'display alert "Alexandria" message "No python3 found on PATH. Set ALEXANDRIA_PYTHON to a Python with the alexandria package installed."'
    exit 1
fi

# argv[0] = "Alexandria" so the Dock binds to this bundle's icon and
# name rather than Python's.
exec -a Alexandria "${PY}" -m alexandria.browse "$@"
EOF
chmod +x "${MACOS}/${APP_NAME}"

# Force LaunchServices to re-read the bundle so the icon refreshes
# without a logout. Best-effort.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "${APP}" >/dev/null 2>&1 || true

echo "built ${APP}"
