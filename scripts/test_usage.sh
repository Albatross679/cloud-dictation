#!/usr/bin/env bash
# Offline actual-source metrics, clients, and native chart typechecking.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/runs/usage-dashboard"
OUT="$(mktemp -d "$ROOT/runs/usage-dashboard/tests.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT
python3 - "$ROOT" "$OUT" <<'PY'
from pathlib import Path
import sys
root, out = map(Path, sys.argv[1:])
source = (root / 'src/client/CloudflareEngine.swift').read_text()
# Compile the real Cloudflare client, not a replacement network implementation.
client = source[:source.index('/// Reads the current provider and its per-provider settings.')]
usage = (root / 'src/client/CloudflareUsageView.swift').read_text()
usage = usage[usage.index('struct CloudflareUsage:'):usage.index('@MainActor')]
(out / 'CloudflareClient.swift').write_text(client + '\n' + usage)
PY
swiftc -O -parse-as-library -o "$OUT/test_usage" \
  "$ROOT/src/client/UsageMetrics.swift" \
  "$ROOT/src/client/CloudProvider.swift" \
  "$ROOT/src/client/CloudflareDirectRequest.swift" \
  "$ROOT/src/client/HuggingFaceRequest.swift" \
  "$ROOT/src/client/OpenRouterRequest.swift" \
  "$OUT/CloudflareClient.swift" \
  "$ROOT/scripts/test_usage_metrics.swift"
"$OUT/test_usage" "$OUT"
swiftc -typecheck "$ROOT/src/client/UsageMetrics.swift" "$ROOT/src/client/UsageDashboard.swift"
if [[ -f "$ROOT/repos/OpenSuperWhisper/OpenSuperWhisper/TranscriptionService.swift" ]]; then
  { printf 'import SwiftUI\n'; python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text())' "$ROOT/repos/OpenSuperWhisper/OpenSuperWhisper/TranscriptionService.swift"; } > "$OUT/TranscriptionService.swift"
  swiftc -O -parse-as-library -o "$OUT/test_service" \
    "$ROOT/src/client/UsageMetrics.swift" "$OUT/TranscriptionService.swift" "$ROOT/scripts/test_usage_service.swift"
  "$OUT/test_service" "$OUT"
  python3 "$ROOT/scripts/test_patch_osw.py"
else
  echo 'Service integration and patch regeneration skipped: run patch_osw.py first.'
fi
