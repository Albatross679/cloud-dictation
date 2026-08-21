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

echo "== Cloudflare direct request =="
"$OUT/test_direct_request"

echo
echo "== Hugging Face and OpenRouter requests =="
"$OUT/test_provider_requests"
