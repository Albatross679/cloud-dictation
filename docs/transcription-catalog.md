# Transcription catalogue and public pricing

Checked 2026-10-01. This is a catalogue of selectable transcription models, not
an accuracy or latency benchmark. No user recordings or paid probe calls were
used to make this table.

**Latency terminology.** `OpenRouter P50` is the provider latency displayed on
the model's OpenRouter page, not end-to-end hotkey-to-paste latency and not a
same-clip comparison. Cloudflare and Hugging Face do not publish a comparable
per-model STT P50 for these routes, so they are explicitly marked unavailable.

**Pricing terminology.** Duration rows are shown in the provider's published
unit (with an exact per-minute multiplication only where it is a fixed
per-second rate). Token rows are deliberately not converted to minutes: the
[OpenRouter STT API](https://openrouter.ai/docs/guides/overview/multimodal/stt)
reports the actual input/output token counts and `usage.cost` for each request.
Hugging Face's `hf-inference` route bills underlying hardware compute time, so
it has no fixed audio-minute price. Cloudflare's published table is the source
for Cloudflare rows; its free 10,000 Neurons/day allocation is separate from
paid usage.

| Provider | Picker key / exact model id | Public price for this route | Published latency |
| --- | --- | --- | --- |
| Cloudflare | `nova-3` / `@cf/deepgram/nova-3` | $0.0052/audio min | Not published comparably |
| Cloudflare | `whisper-turbo` / `@cf/openai/whisper-large-v3-turbo` | $0.0005/audio min | Not published comparably |
| Cloudflare | `whisper` / `@cf/openai/whisper` | $0.0005/audio min | Not published comparably |
| Cloudflare | `whisper-tiny-en` / `@cf/openai/whisper-tiny-en` | Unavailable in current public pricing table | Not published comparably |
| Hugging Face | `whisper-large-v3-turbo` / `openai/whisper-large-v3-turbo` | Compute time × underlying hardware rate; no fixed audio-minute price | Not published comparably |
| Hugging Face | `whisper-large-v3` / `openai/whisper-large-v3` | Compute time × underlying hardware rate; no fixed audio-minute price | Not published comparably |
| OpenRouter | `whisper-large-v3-turbo` / `openai/whisper-large-v3-turbo` | $0.000003/audio sec ($0.00018/audio min) | P50 2.34 s |
| OpenRouter | `whisper-large-v3` / `openai/whisper-large-v3` | $0.000008/audio sec ($0.00048/audio min) | P50 1.56 s |
| OpenRouter | `nova-3` / `deepgram/nova-3` | $0.000072/audio sec ($0.00432/audio min; multilingual $0.000087/sec) | P50 0.66 s |
| OpenRouter | `gpt-4o-mini-transcribe` / `openai/gpt-4o-mini-transcribe` | $1.25/M input tokens + $5/M output tokens | P50 0.79 s |
| OpenRouter | `gpt-4o-transcribe` / `openai/gpt-4o-transcribe` | $2.50/M input tokens + $10/M output tokens | P50 0.79 s |
| OpenRouter | `gemini-3.5-transcribe` / `google/gemini-3.5-transcribe` | $2/M input tokens + $12/M output tokens | P50 2.45 s |
| OpenRouter | `fish-audio/transcribe-1-pro` | $0.0001/audio sec ($0.006/audio min) | P50 0.14 s |
| OpenRouter | `assemblyai/universal-3-5-pro` | $0.000125/audio sec ($0.0075/audio min; prompted $0.000139/sec) | P50 0.93 s |
| OpenRouter | `meta/muse-voice-transcribe-1.0` | $0.00005/audio sec ($0.003/audio min) | P50 1.30 s |
| OpenRouter | `microsoft/mai-transcribe-2` | $0.10/audio hour ($0.0016667/audio min) | P50 0.75 s |
| OpenRouter | `qwen/qwen3-asr-1.7b` | $0.000008/audio sec ($0.00048/audio min) | P50 1.74 s |
| OpenRouter | `qwen/qwen3-asr-0.6b` | $0.000003/audio sec ($0.00018/audio min) | P50 0.81 s |
| OpenRouter | `openai/gpt-transcribe` | $0.000075/audio sec ($0.0045/audio min) | P50 0.77 s |

## Primary sources

- [Cloudflare Workers AI audio pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/#audio-model-pricing)
- [Hugging Face Inference Providers pricing](https://huggingface.co/docs/inference-providers/pricing#hf-inference-cost)
- [OpenRouter speech-to-text request and response schema](https://openrouter.ai/docs/api/api-reference/stt/create-transcription)
- OpenRouter model pages: [Whisper Turbo](https://openrouter.ai/openai/whisper-large-v3-turbo), [Whisper Large V3](https://openrouter.ai/openai/whisper-large-v3), [Nova-3](https://openrouter.ai/deepgram/nova-3), [GPT-4o mini Transcribe](https://openrouter.ai/openai/gpt-4o-mini-transcribe), [GPT-4o Transcribe](https://openrouter.ai/openai/gpt-4o-transcribe), [Gemini 3.5 Transcribe](https://openrouter.ai/google/gemini-3.5-transcribe), [Fish Audio Transcribe 1 Pro](https://openrouter.ai/fish-audio/transcribe-1-pro), [Universal-3.5 Pro](https://openrouter.ai/assemblyai/universal-3-5-pro), [Muse Voice Transcribe 1.0](https://openrouter.ai/meta/muse-voice-transcribe-1.0), [MAI-Transcribe 2](https://openrouter.ai/microsoft/mai-transcribe-2), [Qwen3 ASR 1.7B](https://openrouter.ai/qwen/qwen3-asr-1.7b), [Qwen3 ASR 0.6B](https://openrouter.ai/qwen/qwen3-asr-0.6b), and [GPT Transcribe](https://openrouter.ai/openai/gpt-transcribe).
