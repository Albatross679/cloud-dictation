# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- The compression benchmark under `scripts/compression_bench/` spends real money. Every stage that can reach the worker requires an explicit `--dry-run` or `--live` and refuses to guess; `--dry-run` exercises the same loops offline. `scripts/compression_bench/README.md` is authoritative for the stages, the artifacts each mode owns, and the credentials.
- Only the two probes need silence, and only inside their measurement windows: they read account-level Cloudflare analytics filtered to the same models and request source the dictation app itself uses, so dictating inside a window corrupts that window. The main grid records cost and duration per response and is unaffected. `scripts/compression_bench/quiet_window.py` owns the announcements and the quiet-time estimate.
- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
