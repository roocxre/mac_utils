#!/bin/bash
# Packs build/Mac Utils.app (from scripts/build-app.sh) into build/MacUtils-<version>.dmg with an
# Applications link and a README.
# Usage: scripts/build-dmg.sh
#   MACUTILS_SIGN_IDENTITY="Developer ID Application: …"   also sign the disk image
#   MACUTILS_NOTARY_PROFILE=<notarytool keychain profile>     also notarize and staple it
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/Mac Utils.app"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-0.1.0}"
DMG="$ROOT/build/MacUtils-$VERSION.dmg"

[ -d "$APP" ] || { echo "error: $APP is missing; run scripts/build-app.sh first" >&2; exit 1; }
codesign --verify --deep --strict "$APP"

STAGE="$(mktemp -d "$ROOT/build/dmg.XXXXXX")"
chmod 755 "$STAGE"   # the volume root
trap 'rm -rf "$STAGE"' EXIT
ditto "$APP" "$STAGE/Mac Utils.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/README.txt" <<TEXT
Mac Utils $VERSION

Needs a Mac with Apple Silicon (M1 or later) and macOS 14 Sonoma or later. Intel Macs aren't supported.

To install:
1. Drag Mac Utils to the Applications folder.
2. Open Mac Utils from Applications, tick the components you want and click Install.
3. Follow the Setup screen: it opens the right System Settings pane for each permission and walks
   you through the Gmail sign-in and the Edge extension if you installed them.

If macOS says it can't verify the developer, or that Mac Utils can't be opened:
this build isn't signed by Apple. Click Done (not Move to Trash), open System Settings >
Privacy & Security, scroll down to Security, click Open Anyway next to "Mac Utils", confirm with
your password, then click Open. You only do this once.

Everything is installed in ~/Library/Application Support/MacUtils. To remove it, open Mac Utils >
Manage > Uninstall Everything, then delete the app.
TEXT

rm -f "$DMG"
hdiutil create -volname "Mac Utils $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"

if [ -n "${MACUTILS_SIGN_IDENTITY:-}" ]; then
    codesign --force --timestamp --sign "$MACUTILS_SIGN_IDENTITY" "$DMG"
fi
if [ -n "${MACUTILS_NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$MACUTILS_NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
fi
hdiutil verify "$DMG"
echo "built $DMG ($(du -h "$DMG" | cut -f1))"
