#!/usr/bin/env bash
# Build the patched OpenSuperWhisper with the Cloudflare engine.
#
# Ships under its own bundle id and name so it coexists with an installed
# stock OpenSuperWhisper instead of sharing its preferences and hotkeys.
# Signs with a stable local identity when one exists, falling back to ad hoc.
# The identity matters: macOS ties permission grants to the signature, so ad
# hoc signing invalidates Accessibility and Input Monitoring on every build.
# Create one with scripts/create_signing_identity.sh.
set -euo pipefail

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
export PATH="$HOME/.cargo/bin:$PATH"

BUNDLE_ID="local.clouddictation.OpenSuperWhisper"
SIGN_CN="Cloud Dictation Local Signing"
DISPLAY_NAME="OSW Cloud"

IDENTITY="-"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$SIGN_CN"; then
  IDENTITY="$SIGN_CN"
else
  echo "No local signing identity. Falling back to ad hoc, which resets"
  echo "permissions on every build. Fix with scripts/create_signing_identity.sh"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKOUT="$ROOT/repos/OpenSuperWhisper"

if [ ! -d "$CHECKOUT" ]; then
  echo "No checkout. Run: python3 scripts/patch_osw.py" >&2
  exit 1
fi

cd "$CHECKOUT"

# libwhisper and the autocorrect dylib are native dependencies the Swift
# target links against; run.sh is upstream's builder for both.
if [ ! -f build/libautocorrect_swift.dylib ]; then
  echo "Building native dependencies (cmake + cargo, several minutes)..."
  ./run.sh build
fi

xcodebuild \
  -scheme OpenSuperWhisper \
  -configuration Release \
  -jobs 8 \
  -derivedDataPath build \
  -destination 'platform=macOS,arch=arm64' \
  -clonedSourcePackagesDirPath SourcePackages \
  -skipPackagePluginValidation -skipMacroValidation \
  -quiet \
  CODE_SIGNING_ALLOWED=NO CODE_SIGN_IDENTITY="" CODE_SIGNING_REQUIRED=NO \
  PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" \
  build

APP="$CHECKOUT/build/Build/Products/Release/OpenSuperWhisper.app"
PLIST="$APP/Contents/Info.plist"

/usr/libexec/PlistBuddy -c "Set :CFBundleName $DISPLAY_NAME" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleName string $DISPLAY_NAME" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $DISPLAY_NAME" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string $DISPLAY_NAME" "$PLIST"

# Re-sign ad hoc: editing Info.plist invalidates the existing signature.
codesign --force --deep --sign "$IDENTITY" \
  --entitlements OpenSuperWhisper/OpenSuperWhisper.entitlements "$APP"

xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo
echo "Built:     $APP"
echo "Bundle id: $BUNDLE_ID"
echo "Signed by: $IDENTITY"
