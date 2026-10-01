# Functional verification, 2026-10-01

This check uses synthetic, non-private English, not user recordings. It is a functional smoke test, not an accuracy ranking or a price/latency benchmark. The previous public-source price report is unchanged.

## Request-path results

The actual Swift `OpenRouterClient` and `HuggingFaceClient`, request encoders, and HTTP implementation made the requests. Cloudflare checks used its Swift encoder and `CloudflareClient`. The standalone build extracts the exact Cloudflare client and compressor declarations from `CloudflareEngine.swift`, without replacing their request or audio logic.

The traceable reused clip came from macOS `say`, Samantha voice, 16 kHz mono WAV, 192,973 frames. Its expected text starts "Cloud dictation checks all eight new transcription models. The quick brown fox jumps over the lazy dog." The original generated clip's metadata records its synthesis source. Each successful response contained recognizable known speech, not just a successful HTTP status. Proper-name spellings vary, so these results do not claim exact transcription accuracy.

| Provider | Exact model ID | Result |
| --- | --- | --- |
| OpenRouter | `openai/whisper-large-v3-turbo` | Pass |
| OpenRouter | `openai/whisper-large-v3` | Pass |
| OpenRouter | `deepgram/nova-3` | Pass |
| OpenRouter | `openai/gpt-4o-mini-transcribe` | Pass |
| OpenRouter | `openai/gpt-4o-transcribe` | Pass |
| OpenRouter | `google/gemini-3.5-transcribe` | Pass |
| OpenRouter | `fish-audio/transcribe-1-pro` | Pass after removing returned speaker control tokens |
| OpenRouter | `assemblyai/universal-3-5-pro` | Pass |
| OpenRouter | `meta/muse-voice-transcribe-1.0` | Unavailable to this account, HTTP 403 requires 18+ confirmation at `https://openrouter.ai/settings/preferences` |
| OpenRouter | `microsoft/mai-transcribe-2` | Pass |
| OpenRouter | `qwen/qwen3-asr-1.7b` | Pass |
| OpenRouter | `qwen/qwen3-asr-0.6b` | Pass |
| OpenRouter | `openai/gpt-transcribe` | Pass |
| Hugging Face | `openai/whisper-large-v3-turbo` | Pass |
| Hugging Face | `openai/whisper-large-v3` | Pass |
| Cloudflare Direct | `@cf/deepgram/nova-3` | Pass |
| Cloudflare Direct | `@cf/openai/whisper-large-v3-turbo` | Pass |
| Cloudflare Direct | `@cf/openai/whisper` | Pass |
| Cloudflare Direct | `@cf/openai/whisper-tiny-en` | Pass |

Cloudflare's four base-model results came from the preserved first worker run using the shorter generated phrase "Cloud dictation functional verification. The quick brown fox jumps over thirteen lazy dogs." All four returned that speech, with 13 formatted as digits. That run's stale compiled matcher incorrectly marked them failed, despite the returned text matching the known phrase. No repeat four-model run was needed.

One additional Cloudflare Worker-mode Nova-3 request also passed. OpenRouter's credential-only Test Connection passed and returned all thirteen registered options. A valid connection does not imply access to every model. No model was substituted or removed, and no account attestation was made on the user's behalf.

## Cleanup and audio speed

All configured cleanup options returned nonempty, sane text preserving the known synthetic content:

- Cloudflare Direct: `llama-8b`, `llama-3b`, `granite-micro`, `mistral-24b`.
- Hugging Face: `meta-llama/Llama-3.1-8B-Instruct`, `meta-llama/Llama-3.3-70B-Instruct`, `Qwen/Qwen3-235B-A22B-Instruct-2507`.
- OpenRouter: `google/gemini-2.5-flash`, `openai/gpt-4o-mini`, `meta-llama/llama-3.1-8b-instruct`.

HF and OpenRouter cleanup endpoints were called directly with the app's encoder and prompt. This avoids mistaking the clients' fallback-to-original behavior for a working cleanup model. Cloudflare used the actual Direct API transcription-plus-cleanup path, whose HTTP errors propagate.

The original audio compressor stopped when the player finished, before the time-pitch unit drained its output. On the known clip at 1.5x it wrote 126,976 frames rather than the required 128,649, then threw `noAudioProduced`. The engine would upload the original file instead, so the selected speed did not take effect. The fix renders the buffered tail to the target frame count. The fixed output has 128,649 frames at 16 kHz, and representative transcription-plus-cleanup requests at 1.5x passed for all three providers. Recorder files were not modified.

## Fixes and offline regressions

- Fish speaker tokens such as `<|speaker:0|>` no longer reach pasted text. Filtering is limited to the Fish model and exact speaker-token syntax. A targeted post-fix live request passed without the token.
- HTTP 403 no longer falsely says the API key is invalid. OpenRouter's transcription client identifies the exact unavailable model and preserves the provider's actionable error. HTTP 401 remains an invalid-key error. Meta's current gate stays visible, not silently bypassed.
- Auto-only OpenRouter models omit language even if a stale setting supplies a pinned code. Their existing picker restriction remains intact.
- Audio compression drains the time-pitch tail rather than falling back to the original recording prematurely.

Run `npm test` or `scripts/test_client.sh`. Tests require macOS Swift, AVFoundation, and Python 3, but no credentials or paid requests. They cover existing Cloudflare wire shapes, all thirteen OpenRouter IDs, both HF IDs, language/vocabulary feature declarations, size limits, speaker-token filtering, stubbed HTTP/client errors and cleanup fallback, and all eight accelerated rates at 16/44.1/48 kHz with mono/stereo input. The compressor tests compile its exact private production declarations, not a separate implementation.

## Installed-app limits

The pre-fix installed app was source `3edd9a0`, version `0.1.0`. Its strict deep code-signature check passed, its designated requirement named the retained certificate leaf, and its process was running. Supervisor GUI inspection confirmed Cloudflare Direct/Nova-3 selection, cleanup off, speed 1, every advertised speed choice, Nova-3's ten pinned languages plus Auto, vocabulary, and enabled auto-paste. Settings were not changed.

The fixes built successfully as source `287ca9b`, signed with `Cloud Dictation Local Signing`. The supervisor installed that bundle, and the installed source stamp and strict deep signature check passed with the same certificate-leaf designated requirement. The supervisor quit the old process and verified a new running process and actual Settings window from the installed bundle. The user then changed GUI selections, so no further settings interaction was attempted. Existing upstream Swift concurrency and libomp deployment-target build warnings remain, with no build errors.

Request-path and offline audio tests do not prove physical microphone capture, actual paste into a focused application, or recording history. The supervisor cannot speak into the microphone, and no recorder end-to-end success is claimed. No TCC reset, credential/default overwrite, worker deploy, or destructive upstream reset was performed.

## Single engine bar and shortcuts

The approved follow-up replaces both segmented Engine/Provider bars with one five-choice Engine bar: Parakeet, Whisper, Cloudflare, Hugging Face, OpenRouter. `SpeechRecognitionChoice` derives the visible choice from the existing `selectedEngine`/`cloudProvider` pair. No new persisted preference or destructive migration is needed. Cloud choices retain internal engine key `cloudflare`; local choices keep their existing keys and remember the last cloud provider without using it. Per-provider models and key values are untouched by the picker mapping. The later approved credential-file migration is described below. Provider transitions validate language against the final selection, without loading an intermediate provider. Local Parakeet model controls no longer appear under cloud selections.

Mapping tests cover all old engine/provider combinations, transitions and fallback values. Fresh upstream and existing generated checkout patching were checked, including idempotent reruns. The regenerated Settings source contains one Engine picker and no Provider picker. A later user screenshot reproduced a squeezed vertical Engine label beside the five segments. The picker now hides that redundant visible label, retains explicit accessibility name Engine, and fills the available width. No smaller font or second selector was added. Old picker strings retained in `patch_osw.py` are removal/migration anchors only, never emitted UI.

Shortcut inspection found another reproducible defect: the modifier-only double-tap setting also gated regular keyboard and mouse triggers while its UI was hidden in those modes. The patch limits that gate to modifier mode. `scripts/test_shortcuts.sh` compiles actual pinned/generated dispatch and mouse filtering with isolated dependencies, then runs the full fresh-source/idempotent patch regression. That regression requires a single accessible label-hidden, full-width engine bar. It passed keyboard/mouse toggle start and Escape cancellation, held-release stop-once, middle-button number mapping/filtering, duplicate-event suppression, and modifier double-tap retention. It creates no event taps, posts no input, and records no microphone audio.

The user's later settings were local Whisper, Mouse Button 3 Middle, Hold to Record on, with OpenRouter remembered as the cloud provider. Those latest selections must not be restored to the earlier Cloudflare snapshot. Read-only TCC queries showed microphone, Accessibility and ListenEvent grants; PostEvent had no explicit row. Grant presence is not proof of a live event tap. No double-tap override was stored, so the fixed hidden-setting defect does not explain the supervisor's inconclusive synthetic middle-click GUI test. That test showed no observed recording indicator, and Hold to Record was restored. A loaded local model is needed to decode, not to start and cancel recording. Actual hardware-trigger and microphone-to-paste success remain unverified.

## Scoped staleness audit

Git protects the tracked changes. No recording, credential or model file was removed, and no backup deletion was needed. The audit removed the emitted two-bar UI, old nested-provider menu instructions, unverified accuracy-superlative notes, the misleading invalid-key classification for 403, and the connection-check comment implying every model's availability. The prior public-source price report remains intact. Swift files reported as orphaned by the scanner were false positives: `patch_osw.py` copies them into the generated app, so they were retained.

The external `cloud-dictation-manager` skill still contains worker-only framing, an obsolete assertion that every build resets four grants, and legacy validation paths. Its own stable-signing notes contradict that build-reset claim; the repository build script does not install or reset TCC. These contradictions are reported, not edited. Its generated verified-facts block was neither changed nor refreshed. Compatibility preference keys, generated-checkout migration anchors, every transcription route and Meta's requested entry remain.

## Approved local credential storage

The user subsequently approved plaintext local settings instead of active Keychain storage. Before this change, Settings initialized four provider/connection keys through repeated `SecItemCopyMatching` getters. Startup/setup checks and transcription also read those items, without a cache. Several different items share the same displayed Keychain service name, so repeated permission dialogs need not identify the same credential. The app's signer and bundle identity were unchanged; an ACL mismatch or locked Keychain was not established, and no ACL was inspected or altered.

Credentials now live at `~/Library/Application Support/OSW Cloud/credentials.json`, outside the repo and bundle. The file is mode `0600`, parent directory `0700`, and same-directory rename provides atomic writes. A lock protects the process cache. The user approved that other processes running as the same Mac user, and backups, can still read plaintext keys.

Only a missing file triggers legacy migration. Existing Keychain entries are read with `kSecUseAuthenticationUIFail` and are never modified or deleted. An approval-required item becomes an explicit import notice, not a repeated prompt. A preexisting plain Worker token in UserDefaults is supported without removing unrelated values. Once a local file exists, no automatic Keychain fallback occurs, even if the file is damaged. Save/read errors appear in Settings and block cloud requests rather than pretending an empty key or an unsaved edit succeeded.

Users can paste missing keys into their provider's field or explicitly import supported environment variables from the app process. Existing nonempty keys are not overwritten by environment import. The app does not ask for a Mac password. A malformed file is left intact for repair; Reload local settings is an explicit retry action.

Fake-key regressions passed one-time migration, private modes, per-provider isolation, cached reads, concurrent atomic writes, import notices, corrupt-file preservation, persistence failure and staging-file cleanup. The legacy query's no-UI flag is asserted without executing a real Keychain read. Fresh full source generation and idempotent reruns include the new store and UI messages. No credentials are in the source, app or committed report. A readable canonical legacy key takes precedence over stale Worker defaults; a denied item is not overridden by defaults, and both cases have fake-data regressions.

The supervisor installed source `8d0a35e` and verified a new app process with no observed Keychain prompt. The local file had mode `0600`, parent `0700`, all four credential account keys nonempty and an empty import-needed list; values were not printed. The actual Settings accessibility tree showed one Engine group with all five choices and retained the then-current Whisper/Mouse3/Hold-on settings. The Mac then locked, preventing further automated GUI/physical-trigger checks. The later layout screenshot showed Parakeet selected, so the latest user choice must be preserved rather than restoring the older Whisper snapshot. The external manager skill and older historical reports retain legacy Keychain context; they were not rewritten as current facts.
