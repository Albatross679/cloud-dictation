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

Open `repos/OpenSuperWhisper/OpenSuperWhisper.xcodeproj`, build, then go to Settings > Models > Engine > Cloudflare and paste the endpoint and token.

## API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /models` | bearer | model catalog with live pricing |
| `POST /transcribe` | bearer | audio bytes in, JSON out |

`POST /transcribe` takes raw audio as the request body (`audio/wav`, `audio/mpeg`, `audio/webm`) up to 25 MB.

| Query param | Default | Meaning |
|---|---|---|
| `model` | `nova-3` | `nova-3`, `whisper-turbo`, `whisper` |
| `language` | `auto` | ISO code, or `auto` to detect |
| `cleanup` | off | `1` runs an LLM pass to strip filler words and fix punctuation |
| `cleanup_model` | `llama-8b` | `llama-8b`, `llama-3b`, `granite-micro`, `mistral-24b` |
| `keyterms` | none | comma separated vocabulary the model should spell correctly |
| `instruction` | none | extra cleanup instruction, appended to the system prompt |
| `diarize` | off | `1` labels speakers (nova-3 only) |
| `dictation` | off | `1` interprets spoken punctuation (nova-3 only) |

Server side defaults come from `vars` in `wrangler.jsonc`: `DEFAULT_MODEL`, `DEFAULT_CLEANUP_MODEL`, and `KEYTERMS` (merged with any per request terms).

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

- **Vocabulary boosting does not work on Nova-3 here.** Cloudflare's build rejects `keyterm` ("model does not support keyterm prompting") and also rejects the legacy `keywords` ("use keyterm instead"). `keyterms` is therefore applied as a glossary in the cleanup prompt, and as `initial_prompt` on Whisper, which does support it.
- **The cleanup LLM will substitute words if you let it.** Before the glossary was added it rewrote "stream it from r 2" as "stream it from Redis". The system prompt now forbids guessing at garbled proper nouns. Put your own product names in `KEYTERMS`.
- **Model ids drift.** `@cf/meta/llama-3.1-8b-instruct` currently routes to a variant deprecated on 2026-05-30 and fails through the binding while still working over REST. The registry in `src/core/cleanup.js` pins ids that were verified against the binding.
- `initialize()` only checks that `/models` answers. A wrong model name surfaces on first transcription, not at connect time.
- The worker holds the request open for the whole inference. A 20 minute file on Whisper turbo took 203 seconds.

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
