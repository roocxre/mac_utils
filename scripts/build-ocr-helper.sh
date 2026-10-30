#!/bin/bash
# Builds the screen-text component's recognizer (lib/ocr.swift) for Apple Silicon, macOS 14 and later.
# Usage: scripts/build-ocr-helper.sh [output]   (default: bin/ocr-helper)
# Not stripped: stripping after linking would break the ad-hoc signature swiftc gives it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/bin/ocr-helper}"

mkdir -p "$(dirname "$OUT")"
xcrun --sdk macosx swiftc -O -target arm64-apple-macos14.0 -o "$OUT" "$ROOT/lib/ocr.swift"
chmod 755 "$OUT"
echo "built $OUT"
