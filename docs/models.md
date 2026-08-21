# Choosing a model

Cloudflare is the default provider and the rest of this page is about it. Two others are available under Settings > Models > Engine > Provider; skip to [Other providers](#other-providers) for what they can and cannot do.

Measured against this account, not quoted from vendor docs.

| | Nova-3 | Whisper turbo | Whisper base | Whisper tiny |
|---|---|---|---|---|
| Latency, 9s clip | **433 ms** | 1,382 ms | ~1,250 ms | ~900 ms |
| WER, 3 min varied speech | **0.0%** | 24.3% | not measured | unusable |
| $ per audio minute | 0.0052 | 0.000513 | 0.000453 | **0.0000066** |
| Free tier, audio min/day | 21 | 214 | 243 | **16,556** |

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

Nova-3 is the fastest but is English-plus-nine. Cloudflare's build is far narrower than Deepgram's documented 90+ codes. In Worker mode `GET /models` returns the authoritative list per model; Direct API mode carries the same tested registry in the app because the REST endpoint has no equivalent catalog route. An unsupported pairing is rejected locally by the picker or returns Cloudflare's explicit API error rather than a silent fallback.

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
| Whisper tiny | 36 | $0.0004 |

Nova-3 costs 10.1x Whisper turbo, a difference of $0.28 per hour of speech. The LLM cleanup pass adds about $0.00015 per dictation, which rounds to nothing beside the audio.

Every rate below is read back from billing analytics rather than taken from the docs, because the docs list no price at all for `whisper-tiny-en` and assuming one was wrong by 68x: it bills 0.604 neurons per audio minute, not the 41.14 of Whisper base.

The worker derives neurons per request rather than querying billing, so no extra API credential is needed:

1. Audio duration comes from the model when it reports one (`transcription_info.duration`), else the last word timestamp, else the WAV header.
2. Neurons are duration times the model's measured per minute rate: nova-3 472.73, whisper turbo 46.63, whisper base 41.14, whisper tiny 0.604.

Derived counts estimate what Cloudflare will bill. The dashboard is the authority.

The counter's window is a **UTC day**, matching when the free tier resets. West of UTC that rolls over during the evening, so the readout says "Since 00:00 UTC" rather than "Today".

## Other providers

Hugging Face and OpenRouter transcribe the same recorded WAV through their own APIs and their own keys. Every claim below came from a live request against the vendor rather than from a docs page, and the encoder tests in `scripts/test_provider_requests.swift` pin the wire shapes.

### Hugging Face

Requests go to `POST https://router.huggingface.co/hf-inference/models/<model>` with the WAV as the raw request body and `Content-Type: audio/wav`. The response is `{"text": "..."}`. The same audio also works as `{"inputs": "<base64>"}`; raw bytes are used because they avoid base64's 33 percent upload overhead. The legacy host `api-inference.huggingface.co` no longer resolves.

Only two models are offered, because only two exist to offer. `GET /api/models?pipeline_tag=automatic-speech-recognition&inference_provider=hf-inference` returns exactly `openai/whisper-large-v3-turbo` and `openai/whisper-large-v3`; `whisper-small`, `whisper-tiny.en`, `distil-large-v3`, and `parakeet-tdt-0.6b-v2` all answer HTTP 400 "Model not supported by provider hf-inference".

Two features cannot work here, and both are disabled in the UI with the reason rather than being silently dropped:

| Sent | Response |
|---|---|
| `parameters.language` | 400, "AutomaticSpeechRecognitionPipeline._sanitize_parameters() got an unexpected keyword argument 'language'" |
| `parameters.generation_parameters.prompt` | 400, "The following `model_kwargs` are not used by the model: ['prompt']" |

So Whisper always auto-detects the language here, and the vocabulary list cannot reach the recognizer. It still reaches the cleanup pass as a list of known spellings.

Other Hugging Face providers do serve Whisper: fal-ai, replicate, together, and deepinfra all appear in the model's provider mapping. Each has its own request schema, so none is wired up and none is offered.

### OpenRouter

OpenRouter has had a dedicated speech-to-text endpoint since 2026-05-01, so audio does not go through chat completions as an `input_audio` content part. Requests go to `POST https://openrouter.ai/api/v1/audio/transcriptions` with the body `{"model", "input_audio": {"data": "<base64>", "format": "wav"}, "language"?}`. The response is `{"text", "usage": {"seconds", "total_tokens", "cost"}}`. A `multipart/form-data` form is accepted too for OpenAI SDK compatibility; JSON is used here because the body is then one comparable value that a unit test can assert on.

`GET /api/v1/models?output_modalities=transcription` lists 19 models. The picker offers five:

| Key | Model | Why |
|---|---|---|
| `whisper-large-v3-turbo` | `openai/whisper-large-v3-turbo` | default, cheapest Whisper here |
| `whisper-large-v3` | `openai/whisper-large-v3` | more accurate Whisper |
| `nova-3` | `deepgram/nova-3` | the same model the Cloudflare default uses |
| `gpt-4o-mini-transcribe` | `openai/gpt-4o-mini-transcribe` | strong on proper nouns |
| `gpt-4o-transcribe` | `openai/gpt-4o-transcribe` | most accurate option here |

Language pinning works: `language` takes an ISO-639-1 code and is omitted entirely under auto-detect, because "auto" is not a code. Vocabulary boosting does not, because the endpoint has no prompt or keyterm field. Uploads are capped at 25 MB, which the client checks before encoding so the message names a size rather than surfacing a truncated upload, and upstream providers time out after about 60 seconds.

Pricing units differ per model on OpenRouter's catalogue, so no per-minute figure is quoted here. Every response carries its own `usage.cost` and the activity page is the authority.

### Test Connection

All three providers report three outcomes distinctly, because the fix differs for each:

| Outcome | Cloudflare | Hugging Face | OpenRouter |
|---|---|---|---|
| Key rejected | 401 from a small inference | 401, "Invalid username or password." | 401, "User not found." |
| Host unreachable | transport error | transport error | transport error |
| Reachable, request refused | API error with its code | 400 with the vendor's message | 400 with the vendor's message |

Hugging Face has no credential-only speech route and `/api/whoami-v2` proves a token exists without proving it may call Inference Providers, so the check is a real transcription of a quarter second of generated silence: the same route a dictation takes. OpenRouter has `GET /api/v1/key`, which costs nothing, so that is used instead.

### Cleanup

The cleanup pass follows the provider rather than being skipped. Both new providers expose an OpenAI-shaped `chat/completions` route, so one encoder serves both, with the same system prompt Cloudflare uses.

| Provider | Route | Models |
|---|---|---|
| Hugging Face | `router.huggingface.co/v1/chat/completions` | Llama 3.1 8B (default), Llama 3.3 70B, Qwen3 235B A22B |
| OpenRouter | `openrouter.ai/api/v1/chat/completions` | Gemini 2.5 Flash (default), GPT-4o mini, Llama 3.1 8B |

The gpt-oss family is deliberately absent from the Hugging Face list. It answers 200 with an empty `choices[0].message.content` because its output goes to `reasoning`, which would make the cleanup pass a no-op that looks like it ran.

## Careful points

- **Deepgram Flux is unavailable here.** Websocket only, so it cannot serve a file upload endpoint.
- **Diarization is not offered.** Nova-3 accepts `diarize`, but speaker labels live in `words[].speaker` and `utterances[]`, while the worker returns `alternatives[0].transcript`. Verified on two-speaker audio: output was byte-identical with it on and off. Supporting it means returning structured output, not flipping a flag.
- **The cleanup LLM will substitute words if you let it.** Before the glossary existed it rewrote "stream it from r 2" as "stream it from Redis". The system prompt now forbids guessing at garbled proper nouns. Put your product names in the vocabulary field.
- **Model ids drift.** `@cf/meta/llama-3.1-8b-instruct` routes to a variant deprecated on 2026-05-30 and fails through the AI binding while still working over REST. The registry in `src/core/cleanup.js` pins ids verified against the binding.
- The worker holds the request open for the whole inference. A 20 minute file on Whisper turbo took 203 seconds.
