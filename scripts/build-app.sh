#!/bin/bash
# Builds build/Mac Utils.app: the setup app (installer/), the code it installs (Contents/Resources/payload)
# and uv (Contents/Resources/uv). Needs Apple Silicon and the Command Line Tools; no Xcode.
# Usage: scripts/build-app.sh
#   UV_BINARY=/path/to/uv            the uv to ship; release builds must use the pinned, checksum-verified one
#   MACUTILS_SIGN_IDENTITY="Developer ID Application: …"   sign for distribution instead of ad hoc
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/Mac Utils.app"
RESOURCES="$APP/Contents/Resources"
PAYLOAD="$RESOURCES/payload"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-0.1.0}"

# Left out of the payload: data, models and environments are fetched or built at install time.
EXCLUDE=(.git .venv models bin build dist release-data installer scripts brokers/registry dataflow/extension/icons)
EXCLUDE_ANYWHERE=(__pycache__ .DS_Store .pytest_cache '*.pyc')

[ "$(uname -m)" = arm64 ] || { echo "error: build on Apple Silicon (the app is arm64 only)" >&2; exit 1; }

echo "== app $VERSION"
swift build -c release --package-path "$ROOT/installer"
BIN="$(swift build -c release --package-path "$ROOT/installer" --show-bin-path)/MacUtilsSetup"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$PAYLOAD"
cp "$BIN" "$APP/Contents/MacOS/Mac Utils"
strip -S "$APP/Contents/MacOS/Mac Utils"   # its debug map names the build folders on this Mac (signed below)
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>en</string>
    <key>CFBundleDisplayName</key><string>Mac Utils</string>
    <key>CFBundleExecutable</key><string>Mac Utils</string>
    <key>CFBundleIdentifier</key><string>io.github.roocxre.macutils.setup</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleName</key><string>Mac Utils</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null

echo "== payload"
skipped() {   # true if the payload leaves out this path (relative to the repo)
    local p part parts
    for p in "${EXCLUDE[@]}"; do [[ "$1" == "$p" || "$1" == "$p/"* ]] && return 0; done
    IFS=/ read -ra parts <<< "$1"
    for part in "${parts[@]}"; do
        for p in "${EXCLUDE_ANYWHERE[@]}"; do [[ "$part" == $p ]] && return 0; done
    done
    return 1
}
if git -C "$ROOT" rev-parse -q --verify HEAD >/dev/null 2>&1; then
    LIST="$(mktemp)"
    git -C "$ROOT" ls-files -z | while IFS= read -r -d '' f; do
        if ! skipped "$f" && [ -e "$ROOT/$f" ]; then printf '%s\0' "$f"; fi
    done > "$LIST"
    rsync -a -0 --files-from="$LIST" "$ROOT/" "$PAYLOAD/"
    rm -f "$LIST"
else
    echo "(no git history: copying the folder with the usual excludes)"
    ARGS=()
    for p in "${EXCLUDE[@]}"; do ARGS+=("--exclude=/$p"); done
    for p in "${EXCLUDE_ANYWHERE[@]}"; do ARGS+=("--exclude=$p"); done
    rsync -a "${ARGS[@]}" "$ROOT/" "$PAYLOAD/"
fi
"$ROOT/scripts/build-ocr-helper.sh" "$PAYLOAD/bin/ocr-helper"

echo "== uv"
UV="${UV_BINARY:-}"
if [ -z "$UV" ]; then
    UV="$(command -v uv || true)"
    echo "warning: UV_BINARY isn't set, so this ships the uv on PATH ($UV)." >&2
    echo "warning: release builds must ship the pinned uv release, checked against its SHA-256." >&2
fi
[ -n "$UV" ] && [ -x "$UV" ] || { echo "error: no uv to ship (set UV_BINARY)" >&2; exit 1; }
cp "$UV" "$RESOURCES/uv"
chmod 755 "$RESOURCES/uv"
SHIPPED="$("$RESOURCES/uv" --version | awk 'NR == 1 { print $2 }')"
PINNED="$(sed -n 's/^UV_VERSION=\([^ ]*\).*/\1/p' "$ROOT/install.sh" 2>/dev/null | head -1)"
echo "uv $SHIPPED (install.sh pins ${PINNED:-nothing})"
if [ -n "$PINNED" ] && [ "$SHIPPED" != "$PINNED" ]; then
    # A signed build is a release: it must ship the pinned uv.
    [ -z "${MACUTILS_SIGN_IDENTITY:-}" ] || { echo "error: uv $SHIPPED isn't the pinned $PINNED" >&2; exit 1; }
    echo "warning: uv $SHIPPED isn't the pinned $PINNED" >&2
fi

echo "== sign"
xattr -cr "$APP"   # Finder info and other extended attributes break the signature
if [ -n "${MACUTILS_SIGN_IDENTITY:-}" ]; then
    SIGN=(codesign --force --options runtime --timestamp --sign "$MACUTILS_SIGN_IDENTITY")
else
    SIGN=(codesign --force --sign -)
fi
# Nested executables first (uv, bin/ocr-helper and any other Mach-O in the payload), then the app.
find "$RESOURCES" -type f -perm +111 -print0 | while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q '^Mach-O'; then "${SIGN[@]}" "$f"; fi
done
"${SIGN[@]}" "$APP"
codesign --verify --deep --strict "$APP"

echo "built $APP ($(du -sh "$APP" | cut -f1))"
