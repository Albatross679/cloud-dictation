#!/usr/bin/env bash
# Compiles and runs the Swift client's unit tests. The direct-API request
# encoder imports only Foundation, so it is testable without Xcode's project
# or the app's whisper.cpp and Rust dependencies.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

swiftc -O -o "$OUT/test_direct_request" \
  "$ROOT/src/client/CloudflareDirectRequest.swift" \
  "$ROOT/scripts/test_direct_request.swift"

"$OUT/test_direct_request"
