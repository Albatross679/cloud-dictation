# Attribution

## This project is a fork of OpenSuperWhisper

**[Starmel/OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper)** — MIT License, Copyright (c) 2024 OpenSuperWhisper.

Everything a dictation app actually has to get right comes from that project: the global hotkey, the audio recorder, the microphone handling, pasting into the focused application, the transcription history, the settings interface, and the local Whisper and Parakeet engines. This repository adds one transcription engine that sends audio to a Cloudflare Worker, and the Worker that answers it.

The fork is deliberately thin. `scripts/patch_osw.py` clones upstream at a pinned commit and applies each change as an exact string replacement, so upstream's code is fetched at build time rather than copied here, and every modification this project makes is visible in one file.

If you want a dictation app that runs entirely on your own machine, use OpenSuperWhisper directly. It is the better choice for most people, and it ships a notarized installer.

## Bundled through the upstream build

The application links these projects, which are pulled in by the upstream Xcode project and its submodules:

| Project | License | Copyright |
|---|---|---|
| [whisper.cpp](https://github.com/ggerganov/whisper.cpp) | MIT | 2023-2026 The ggml authors |
| [autocorrect](https://github.com/huacnlee/autocorrect) | MIT | 2020 Jason Lee |
| [FluidAudio](https://github.com/FluidInference/FluidAudio) | Apache 2.0 | FluidInference |
| [GRDB.swift](https://github.com/groue/GRDB.swift) | MIT | 2015-2025 Gwendal Roué |
| [KeyboardShortcuts](https://github.com/sindresorhus/KeyboardShortcuts) | MIT | Sindre Sorhus |

## Models

Transcription runs on Cloudflare Workers AI. The models are operated by Cloudflare and are not distributed with this software:

- **Deepgram Nova-3**, proprietary, via `@cf/deepgram/nova-3`
- **OpenAI Whisper**, MIT, via `@cf/openai/whisper-large-v3-turbo`, `@cf/openai/whisper`, and `@cf/openai/whisper-tiny-en`

Deepgram's Keyterm Prompting, which this project uses for vocabulary boosting, is Deepgram's feature and documented at [developers.deepgram.com](https://developers.deepgram.com/docs/keyterm).
