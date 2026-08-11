# cloud-dictation

Dictation for macOS that transcribes on Cloudflare Workers AI instead of on your Mac.

Two pieces:

- A Cloudflare Worker that takes audio and returns text.
- A `CloudflareEngine` added to [OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper) (MIT), which supplies the hotkey, recorder, and paste-into-focused-app behavior.

OpenSuperWhisper already abstracts inference behind a 6 member `TranscriptionEngine` protocol, so the online engine drops in beside the local Whisper and Parakeet engines. Nothing upstream is rewritten.

## Quickstart

```bash
cd ~/repos/cloud-dictation && npm install && npx wrangler deploy
```

Then set the shared secret and patch the app:

```bash
openssl rand -hex 24 | tee .auth-token.local | npx wrangler secret put AUTH_TOKEN
```

```bash
python3 scripts/patch_osw.py
```

Then build and install the app:

```bash
./scripts/build_app.sh
```

Copy `repos/OpenSuperWhisper/build/Build/Products/Release/OpenSuperWhisper.app` to `/Applications`, then set Settings > Models > Engine > Cloudflare and paste the endpoint and token. Terms go in Settings > Transcription > Vocabulary.

## Build requirements

| Need | Why | Install |
|---|---|---|
| Xcode | the app is an `.xcodeproj` | App Store, then `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer && sudo xcodebuild -license accept` and `xcodebuild -runFirstLaunch` |
| cmake | builds `libwhisper` | `brew install cmake` |
| Rust | builds the `asian-autocorrect` dylib the bridging header imports | `curl https://sh.rustup.rs -sSf \| sh` |
| libomp | linked by `libwhisper` | `brew install libomp` |

The local Whisper engine compiles even when only the Cloudflare engine is used, so all four are required.

`build_app.sh` signs ad hoc and ships under bundle id `local.clouddictation.OpenSuperWhisper` with the display name `OSW Cloud`, so it coexists with a stock OpenSuperWhisper install rather than sharing its preferences. If both are installed, give them different record hotkeys: the default is Option + backtick in each.

## Packaging

```bash
./scripts/make_dmg.sh
```

Produces a drag-to-install `runs/OSW Cloud.dmg` of about 8 MB. Unsigned by default, which installs on the machine that built it and is refused by Gatekeeper everywhere else.

Distribution needs an Apple Developer ID, after which the same script signs, notarizes, and staples:

```bash
./scripts/make_dmg.sh "Developer ID Application: Your Name (TEAMID)" your-notary-profile
```

The DMG format is not what makes an app distributable; notarization is. Upstream ships a notarized 11 MB DMG, which is why installing OpenSuperWhisper never required Xcode. The build toolchain (Xcode, cmake, Rust, libomp) is a cost for whoever builds the app, not for whoever installs it.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /models` | bearer | model catalog with live pricing |
| `POST /transcribe` | bearer | audio bytes in, JSON out |

`POST /transcribe` takes raw audio as the request body (`audio/wav`, `audio/mpeg`, `audio/webm`) up to 25 MB.

| Query param | Default | Meaning |
|---|---|---|
| `model` | `nova-3` | `nova-3`, `whisper-turbo`, `whisper`, `whisper-tiny-en` |
| `language` | `auto` | ISO code, or `auto` to detect |
| `cleanup` | off | `1` runs an LLM pass to strip filler words and fix punctuation |
| `cleanup_model` | `llama-8b` | `llama-8b`, `llama-3b`, `granite-micro`, `mistral-24b` |
| `vocabulary` | none | comma separated terms to spell correctly (`initial_prompt` is accepted as an alias) |
| `instruction` | none | extra cleanup instruction, appended to the system prompt |
| `diarize` | off | `1` labels speakers (nova-3 only) |
| `dictation` | off | `1` interprets spoken punctuation (nova-3 only) |

`GET /usage` reports today's audio seconds, neurons, free tier fraction, and billable dollars, plus 30 days of history. Counts live in a Durable Object so concurrent dictations cannot lose an increment. `POST /usage/reset` clears them.

Server side defaults come from `vars` in `wrangler.jsonc`: `DEFAULT_MODEL`, `DEFAULT_CLEANUP_MODEL`, and `INITIAL_PROMPT`.

## Languages

Every engine's picker follows its own selected model, and no engine offers a language it will not honor:

| Engine | Picker follows |
|---|---|
| Cloudflare | the worker's per-model list, cached on connect |
| Whisper (local) | the downloaded `.bin`: `ggml-*.en.bin` is English only |
| Parakeet (local) | the Parakeet version, and the choice now reaches the engine |

Parakeet's `transcribe` has always taken a `language:` argument; upstream never passed one, so the filtered picker was decorative and every clip was auto-detected.

For the cloud engine specifically, the worker is the authority. `GET /models` returns a `languages` array per model, the app caches it on connect, and a language a model cannot serve is rejected with a 400 naming the supported set rather than a 502 from the provider.

| Model | Languages | Measured on Chinese audio |
|---|---|---|
| `whisper-turbo` | any the client can display | correct, simplified characters |
| `nova-3` | `en es fr de it pt nl hi ru ja` only | **hard error**, no such model/language combination |
| `whisper` | auto-detect only, discards the setting | correct, but returned traditional characters |
| `whisper-tiny-en` | `en` | hallucinates English, unusable |

Nova-3 is the default and the fastest, but it is English-plus-nine. **For anything outside that set, use `whisper-turbo`.** Cloudflare's Nova-3 build is far narrower than Deepgram's own documented range of 90+ codes.

## Vocabulary

One field, Settings > Transcription > **Vocabulary**, is a **term list**: comma or newline separated, no sentences. Every entry is taken verbatim, so `kubectl` stays lowercase and `R2` keeps its digit. Multi-word entries stay whole, which matters because Deepgram boosts `Workers AI` as a phrase. Duplicates are folded case-insensitively and the list is capped at Deepgram's 100.

Each model consumes the same list in the way it can:

| Model | How the list is used |
|---|---|
| `nova-3` | Deepgram Keyterm Prompting, boosts each term at recognition time |
| `whisper-turbo` | joined into a `Glossary: ...` decoder prompt |
| `whisper`, `whisper-tiny-en` | recognizer ignores it; reaches the cleanup pass as known spellings |

Measured on the same audio, vocabulary off then on:

| Model | Without | With |
|---|---|---|
| `nova-3` | "from r two" | "from R2" |
| `nova-3` | "in vectorize ... hyperdrive" | "in Vectorize ... Hyperdrive" |
| `whisper-turbo` | "through hyperdrive" | "through Hyperdrive" |

**Nova-3 boosting needs a pinned language.** With `language=auto` the request routes to the multilingual Nova-3, which rejects `keyterm` outright. The worker drops the terms in that case and explains why in `terms_skipped_reason` rather than failing the transcription.

`keywords`, the pre-Nova-3 parameter, is refused: "Keywords are not supported for Nova-3. Please use keyterm instead."

## What an hour costs

60 audio minutes, free tier excluded:

| Model | Neurons | Cost |
|---|---|---|
| Nova-3 | 28,362 | $0.3120 |
| Whisper turbo | 2,798 | $0.0308 |
| Whisper base | 2,468 | $0.0272 |

Nova-3 is 10.1x Whisper turbo, a difference of $0.28 per hour of speech.

## Metering

Neurons are Cloudflare's billing unit. The worker derives them per request rather than querying billing, so no extra API credential is needed:

1. Audio duration comes from the model when it reports one (`transcription_info.duration`), else the last word timestamp, else the WAV header.
2. Neurons are duration times the model's published per minute rate. Verified against billing analytics: nova-3 bills 472.7 per audio minute, whisper turbo 46.6.

## Choosing a model

Measured on this account, not quoted from docs.

| | Nova-3 | Whisper turbo | Whisper base |
|---|---|---|---|
| Latency, 9s clip | **433 ms** | 1,382 ms | ~1,250 ms |
| WER on 3 min varied speech | **0.0%** | 24.3% | not measured |
| $ per audio minute | 0.0052 | **0.000513** | 0.000453 |
| Free tier, audio min/day | 21 | **214** | 243 |
| Punctuation and casing | yes | yes | yes |
| Diarization | yes | no | no |

Nova-3 is the default. It is 3x lower latency, which is what dictation actually depends on, and it does not drop content.

**Whisper turbo silently drops words on longer audio.** On 3 minutes of varied technical speech it lost 89 of 412 words. This reproduces with `vad_filter` both on and off. Use it for short clips or when cost dominates, not for meetings.

## Cost

10,000 neurons per day are free on both the free and paid plans.

| Daily dictation | Whisper turbo | Nova-3 |
|---|---|---|
| 30 min | $0 | $1.38/mo |
| 1 hr | $0 | $6.06/mo |
| 2 hr | $0 | $15.42/mo |

The LLM cleanup pass adds roughly $0.00015 per dictation, which rounds to nothing next to the audio.

## Careful points

- **Deepgram Flux is not available here.** It is websocket only, so it cannot serve a file upload endpoint.
- Derived neuron counts are an estimate of what Cloudflare will bill, not a reading of the invoice. The dashboard is the authority.
- **The cleanup LLM will substitute words if you let it.** Before the glossary was added it rewrote "stream it from r 2" as "stream it from Redis". The system prompt now forbids guessing at garbled proper nouns. Put your own product names in `KEYTERMS`.
- **Model ids drift.** `@cf/meta/llama-3.1-8b-instruct` currently routes to a variant deprecated on 2026-05-30 and fails through the binding while still working over REST. The registry in `src/core/cleanup.js` pins ids that were verified against the binding.
- `initialize()` only checks that `/models` answers. A wrong model name surfaces on first transcription, not at connect time.
- The worker holds the request open for the whole inference. A 20 minute file on Whisper turbo took 203 seconds.
- Run `scripts/create_signing_identity.sh` once before the first build. It creates a self-signed certificate so every build shares one signature, which is what lets macOS keep the Accessibility and Input Monitoring grants. Without it the build falls back to ad hoc and every rebuild silently invalidates them.

## Structure

```text
src/
├── index.js            router and auth gate
├── api/
│   ├── auth.js         bearer check, constant time compare
│   └── transcribe.js   request handling, audio limits, cleanup orchestration
├── core/
│   ├── models.js       STT registry: ids, pricing, per model input shapes
│   └── cleanup.js      LLM registry and the cleanup system prompt
└── client/
    └── CloudflareEngine.swift   TranscriptionEngine conformance for OpenSuperWhisper

scripts/
└── patch_osw.py        clones OpenSuperWhisper and applies the engine, idempotent

repos/                  gitignored checkout, created by the patch script
```
