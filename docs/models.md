# Choosing a model

Measured against this account, not quoted from vendor docs.

| | Nova-3 | Whisper turbo | Whisper base | Whisper tiny |
|---|---|---|---|---|
| Latency, 9s clip | **433 ms** | 1,382 ms | ~1,250 ms | ~900 ms |
| WER, 3 min varied speech | **0.0%** | 24.3% | not measured | unusable |
| $ per audio minute | 0.0052 | **0.000513** | 0.000453 | not listed |
| Free tier, audio min/day | 21 | **214** | 243 | 243 |

**Nova-3 is the default.** Its latency is a third of the alternatives, which is what dictation actually depends on, and it does not drop content.

**Whisper turbo silently drops words on longer audio.** On 3 minutes of varied technical speech it lost 89 of 412 words, reproducibly, with `vad_filter` both on and off. Use it for short clips, for non-English, or when cost dominates. Not for meetings.

## Languages

Every picker in the app follows its own selected model, and no engine offers a language it will not honor.

| Model | Languages | Measured on Chinese audio |
|---|---|---|
| `whisper-turbo` | any the client can display | correct, simplified characters |
| `nova-3` | `en es fr de it pt nl hi ru ja` only | **hard error** |
| `whisper` | auto-detect only, discards the setting | correct, but traditional characters |
| `whisper-tiny-en` | `en` | hallucinates English, unusable |

Nova-3 is the fastest but is English-plus-nine. Cloudflare's build is far narrower than Deepgram's documented 90+ codes. `GET /models` returns the authoritative list per model; the app caches it on connect, and an unsupported pairing returns a 400 naming the supported set rather than a 502 from the provider.

The local engines follow their own models too: local Whisper narrows to English for a `ggml-*.en.bin`, and Parakeet follows its version. Parakeet's `transcribe` has always taken a `language:` argument that upstream never passed, so its filtered picker was decorative and every clip was auto-detected until this fork supplied it.

## Vocabulary

Settings > Transcription > **Vocabulary** is a term list, comma or newline separated, no sentences. Entries are taken verbatim, so `kubectl` stays lowercase and `R2` keeps its digit. Multi-word entries stay whole, which matters because Deepgram boosts `Workers AI` as a phrase. Duplicates fold case-insensitively; the list caps at Deepgram's 100.

Each model uses the same list in the way it can:

| Model | How |
|---|---|
| `nova-3` | Deepgram Keyterm Prompting, boosted at recognition |
| `whisper-turbo` | joined into a `Glossary: ...` decoder prompt |
| `whisper`, `whisper-tiny-en` | recognizer ignores it, cleanup pass sees it as known spellings |

Measured on identical audio, off then on:

| Model | Without | With |
|---|---|---|
| `nova-3` | "from r two" | "from R2" |
| `nova-3` | "in vectorize ... hyperdrive" | "in Vectorize ... Hyperdrive" |
| `whisper-turbo` | "through hyperdrive" | "through Hyperdrive" |

**Nova-3 boosting needs a pinned language.** On `language=auto` the request routes to the multilingual Nova-3, which rejects `keyterm` outright. The worker then drops the terms and explains why in `terms_skipped_reason` rather than failing the transcription.

`keywords`, the pre-Nova-3 parameter, is always refused: "Keywords are not supported for Nova-3. Please use keyterm instead."

## Cost and metering

Neurons are Cloudflare's billing unit. 10,000 per day are free on both the free and paid plans.

An hour of audio, free tier excluded:

| Model | Neurons | Cost |
|---|---|---|
| Nova-3 | 28,362 | $0.3120 |
| Whisper turbo | 2,798 | $0.0308 |
| Whisper base | 2,468 | $0.0272 |

Nova-3 costs 10.1x Whisper turbo, a difference of $0.28 per hour of speech. The LLM cleanup pass adds about $0.00015 per dictation, which rounds to nothing beside the audio.

The worker derives neurons per request rather than querying billing, so no extra API credential is needed:

1. Audio duration comes from the model when it reports one (`transcription_info.duration`), else the last word timestamp, else the WAV header.
2. Neurons are duration times the model's published per minute rate, verified against billing analytics: nova-3 bills 472.7 per audio minute, whisper turbo 46.6.

Derived counts estimate what Cloudflare will bill. The dashboard is the authority.

## Careful points

- **Deepgram Flux is unavailable here.** Websocket only, so it cannot serve a file upload endpoint.
- **Diarization is not offered.** Nova-3 accepts `diarize`, but speaker labels live in `words[].speaker` and `utterances[]`, while the worker returns `alternatives[0].transcript`. Verified on two-speaker audio: output was byte-identical with it on and off. Supporting it means returning structured output, not flipping a flag.
- **The cleanup LLM will substitute words if you let it.** Before the glossary existed it rewrote "stream it from r 2" as "stream it from Redis". The system prompt now forbids guessing at garbled proper nouns. Put your product names in the vocabulary field.
- **Model ids drift.** `@cf/meta/llama-3.1-8b-instruct` routes to a variant deprecated on 2026-05-30 and fails through the AI binding while still working over REST. The registry in `src/core/cleanup.js` pins ids verified against the binding.
- The worker holds the request open for the whole inference. A 20 minute file on Whisper turbo took 203 seconds.
