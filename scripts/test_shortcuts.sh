#!/usr/bin/env bash
# Run after patch_osw.py. Tests the pinned, generated shortcut and mouse logic
# with no input posting, microphone recording, real preferences or event taps.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$ROOT/repos/OpenSuperWhisper/OpenSuperWhisper"
if [[ ! -f "$APP/ShortcutManager.swift" || ! -f "$APP/MouseButtonMonitor.swift" ]]; then
  echo 'Generate the pinned app source with python3 scripts/patch_osw.py first.' >&2
  exit 1
fi
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
python3 - "$ROOT" "$APP" "$OUT" <<'PY'
from pathlib import Path
import sys
root, app, out = map(Path, sys.argv[1:])
mouse = (app / 'MouseButtonMonitor.swift').read_text()
(out / 'mouse.swift').write_text(mouse + '\n' + (root / 'scripts/test_mouse_button.swift').read_text())
# Keep production dispatch code verbatim; provide the external shortcut-library
# API and recording dependencies as isolated test doubles.
manager = (app / 'ShortcutManager.swift').read_text().replace('import KeyboardShortcuts\n', '')
(out / 'shortcuts.swift').write_text(mouse[:mouse.index('class MouseButtonMonitor')]
    + manager + '\n' + (root / 'scripts/test_shortcuts.swift').read_text())
PY
swiftc -O -parse-as-library -o "$OUT/mouse" "$OUT/mouse.swift"
swiftc -O -parse-as-library -o "$OUT/shortcuts" "$OUT/shortcuts.swift"
"$OUT/mouse"
"$OUT/shortcuts"
