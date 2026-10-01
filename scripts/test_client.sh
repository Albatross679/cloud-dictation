#!/usr/bin/env bash
# Compiles and runs the Swift client's unit tests. The request encoders import
# only Foundation, so they are testable without Xcode's project or the app's
# whisper.cpp and Rust dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

swiftc -O -o "$OUT/test_direct_request" \
  "$ROOT/src/client/CloudflareDirectRequest.swift" \
  "$ROOT/scripts/test_direct_request.swift"

swiftc -O -o "$OUT/test_provider_requests" \
  "$ROOT/src/client/CloudProvider.swift" \
  "$ROOT/src/client/HuggingFaceRequest.swift" \
  "$ROOT/src/client/OpenRouterRequest.swift" \
  "$ROOT/scripts/test_provider_requests.swift"

swiftc -O -o "$OUT/test_provider_clients" \
  "$ROOT/src/client/CloudProvider.swift" \
  "$ROOT/src/client/HuggingFaceRequest.swift" \
  "$ROOT/src/client/OpenRouterRequest.swift" \
  "$ROOT/scripts/test_provider_clients.swift"

# Compile the private AVFoundation compressor verbatim with its tests. Keep
# extraction bounded by declarations so no app preferences or native engines
# are stubbed, and a moved declaration fails rather than testing stale code.
python3 - "$ROOT" "$OUT" <<'PY'
from pathlib import Path
import sys
root, out = map(Path, sys.argv[1:])
source = (root / 'src/client/CloudflareEngine.swift').read_text()
start = source.index('private final class CloudflarePlaybackCompletion:')
end = source.index('/// Transcribes through a Cloudflare Worker', start)
(out / 'test_audio_compressor.swift').write_text(
    'import Foundation\nimport AVFoundation\n' + source[start:end]
    + (root / 'scripts/test_audio_compressor.swift').read_text()
)
PY
swiftc -O -parse-as-library -o "$OUT/test_audio_compressor" "$OUT/test_audio_compressor.swift"

echo "== Cloudflare direct request =="
"$OUT/test_direct_request"

echo
echo "== Hugging Face and OpenRouter requests =="
"$OUT/test_provider_requests"

echo
echo "== Provider clients, stubbed HTTP =="
"$OUT/test_provider_clients"

echo
echo "== Audio speed, offline AVFoundation =="
"$OUT/test_audio_compressor"
