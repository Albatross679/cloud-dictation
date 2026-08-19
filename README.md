# cloud-dictation

Dictation for macOS that transcribes on Cloudflare Workers AI instead of on your Mac.

Hold a hotkey, speak, and the text lands in whatever app has focus.

> ### Built on OpenSuperWhisper
>
> This is a fork of **[Starmel/OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper)** (MIT), which provides the hotkey, recorder, paste-into-focused-app behavior, history, settings, and the local Whisper and Parakeet engines. This repository adds one thing: a transcription engine that calls a Cloudflare Worker, plus the Worker itself.
>
> Upstream abstracts inference behind a six member `TranscriptionEngine` protocol, so the cloud engine drops in beside the local ones and nothing upstream is rewritten. If you want dictation that runs entirely on your own machine, **use OpenSuperWhisper directly** — it is the better choice for most people and ships a notarized installer.
>
> Full attribution, including the projects bundled through the upstream build, is in [NOTICE.md](NOTICE.md).

## Why

Commercial dictation apps run $8 to $15 a month. Cloudflare gives 10,000 neurons per day free, on the free plan and the paid plan alike.

| Daily dictation | This, on Whisper turbo | This, on Nova-3 | Wispr Flow |
|---|---|---|---|
| 30 min | **$0** | $1.38/mo | $15/mo |
| 1 hr | **$0** | $6.06/mo | $15/mo |

Whisper turbo stays inside the free tier up to 3.5 hours of speech per day.

## Install

```bash
npm install && npx wrangler deploy
```

```bash
openssl rand -hex 24 | tee .auth-token.local | npx wrangler secret put AUTH_TOKEN
```

```bash
./scripts/create_signing_identity.sh && python3 scripts/patch_osw.py && ./scripts/build_app.sh
```

Copy the built app from `repos/OpenSuperWhisper/build/Build/Products/Release/` to `/Applications`, or run `./scripts/make_dmg.sh` for an installer.

Needs Xcode, `cmake`, Rust, and `libomp`. See [docs/building.md](docs/building.md).

## Configure

In the app, Settings > Models > Engine > **Cloudflare**, then paste the worker URL and the token from `.auth-token.local`.

Two settings worth knowing:

- **Transcription > Language.** Nova-3 serves ten languages and errors on the rest. For anything else pick `whisper-turbo`.
- **Transcription > Vocabulary.** A comma separated term list, not prose. `R2, Kubernetes, Workers AI`. It measurably fixes proper nouns.
- **Cloudflare > Audio speed.** Choose `1`, `1.25`, `1.5`, `1.75`, `2`, `2.25`, `2.5`, `2.75`, or `3`; default `1` uploads the original recording unchanged. Higher speeds preserve pitch and cut billed audio minutes, but trade accuracy for cost—see the measured WER deltas in [asr-compression-cost](https://github.com/Albatross679/asr-compression-cost).

## Structure

```text
src/
├── index.js      router and auth gate
├── api/          request handling, auth, usage counter
├── core/         model registry, cleanup, vocabulary, language, metering
└── client/       the Swift engine added to OpenSuperWhisper

scripts/          patch, build, package, signing identity
docs/             the detail
runs/             generated, gitignored
repos/            upstream checkout, generated, gitignored
```

## Docs

- [docs/models.md](docs/models.md) — which model to pick, languages, vocabulary, measured accuracy and cost
- [docs/api.md](docs/api.md) — worker endpoints and parameters
- [docs/building.md](docs/building.md) — build requirements, signing, packaging, reproducibility

## License

MIT, see [LICENSE](LICENSE). Derived from [OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper), MIT, Copyright (c) 2024 OpenSuperWhisper. Third party attribution in [NOTICE.md](NOTICE.md).
