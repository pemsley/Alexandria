#!/bin/sh
# Build a macOS .icns file from the app's SVG icon.
#
# Rasterises the SVG at every size the Finder / Dock expects, assembles
# an .iconset bundle, then hands it to /usr/bin/iconutil to produce a
# single .icns suitable for use in an .app bundle's Contents/Resources/.
#
# Requires: rsvg-convert (Homebrew: `brew install librsvg`) and the
# system iconutil that ships with macOS.

set -eu

APP_ID="io.github.pemsley.Alexandria"
REPO=$(cd "$(dirname "$0")" && pwd)

SVG="${REPO}/data/${APP_ID}.svg"
ICNS="${REPO}/data/${APP_ID}.icns"
ICONSET=$(mktemp -d -t alexandria-iconset.XXXXXX)/${APP_ID}.iconset

RSVG=/opt/homebrew/bin/rsvg-convert
ICONUTIL=/usr/bin/iconutil

if [ ! -x "${RSVG}" ]; then
    echo "error: ${RSVG} not found; try \`brew install librsvg\`" >&2
    exit 1
fi
if [ ! -x "${ICONUTIL}" ]; then
    echo "error: ${ICONUTIL} not found (expected with macOS)" >&2
    exit 1
fi
if [ ! -f "${SVG}" ]; then
    echo "error: ${SVG} not found" >&2
    exit 1
fi

mkdir -p "${ICONSET}"

# name  pixels
render() {
    name=$1
    px=$2
    "${RSVG}" -w "${px}" -h "${px}" -a -o "${ICONSET}/${name}" "${SVG}"
}

render icon_16x16.png       16
render icon_16x16@2x.png    32
render icon_32x32.png       32
render icon_32x32@2x.png    64
render icon_128x128.png    128
render icon_128x128@2x.png 256
render icon_256x256.png    256
render icon_256x256@2x.png 512
render icon_512x512.png    512
render icon_512x512@2x.png 1024

"${ICONUTIL}" -c icns -o "${ICNS}" "${ICONSET}"
rm -rf "$(dirname "${ICONSET}")"

echo "wrote ${ICNS}"
