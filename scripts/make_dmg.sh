#!/usr/bin/env bash
# Package the built app as a drag-to-install DMG.
#
# Signs and notarizes when a Developer ID identity exists, and otherwise
# produces an unsigned DMG that installs fine on this Mac but is refused by
# Gatekeeper elsewhere. Notarization is what makes the DMG distributable, not
# the DMG format itself.
#
#   make_dmg.sh                              unsigned, or signed if an identity exists
#   make_dmg.sh "Developer ID Application: Name (TEAMID)" <keychain-profile>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/repos/OpenSuperWhisper/build/Build/Products/Release/OpenSuperWhisper.app"
VOL_NAME="OSW Cloud"
DMG="$ROOT/runs/OSW Cloud.dmg"

IDENTITY="${1:-}"
KEYCHAIN_PROFILE="${2:-}"

[ -d "$APP" ] || { echo "No built app. Run scripts/build_app.sh first." >&2; exit 1; }

# Fall back to whatever Developer ID is already in the keychain.
if [ -z "$IDENTITY" ]; then
  IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
    | grep "Developer ID Application" | head -1 | sed 's/.*"\(.*\)"/\1/') || true
fi

mkdir -p "$ROOT/runs"
rm -f "$DMG"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/$VOL_NAME.app"
ln -s /Applications "$STAGE/Applications"

if [ -n "$IDENTITY" ]; then
  echo "Signing with: $IDENTITY"
  codesign --force --deep --timestamp --options runtime \
    --entitlements "$ROOT/repos/OpenSuperWhisper/OpenSuperWhisper/OpenSuperWhisper.entitlements" \
    --sign "$IDENTITY" "$STAGE/$VOL_NAME.app"
else
  echo "No Developer ID identity found. Building an UNSIGNED dmg."
fi

hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "Built: $DMG ($(du -h "$DMG" | cut -f1))"

if [ -z "$IDENTITY" ]; then
  cat <<'EOF'

UNSIGNED. It installs on this Mac, and Gatekeeper refuses it everywhere else.
A recipient would have to run:

  xattr -dr com.apple.quarantine "/Applications/OSW Cloud.app"

Distributing means an Apple Developer account, then rerunning this with the
identity and a notarytool keychain profile.
EOF
  exit 0
fi

if [ -n "$KEYCHAIN_PROFILE" ]; then
  echo "Notarizing..."
  xcrun notarytool submit "$DMG" --wait --keychain-profile "$KEYCHAIN_PROFILE"
  xcrun stapler staple "$DMG"
  echo "Notarized and stapled."
else
  codesign --force --sign "$IDENTITY" "$DMG"
  echo "Signed but NOT notarized: pass a notarytool keychain profile as the second argument."
fi
