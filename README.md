# cloud-dictation

Dictation for macOS that transcribes on Cloudflare Workers AI instead of on your Mac.

Hold a hotkey, speak, and the text lands in whatever app has focus. A Cloudflare Worker does the transcription; [OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper) (MIT) supplies the recorder, hotkey, and paste. Upstream abstracts inference behind a six member `TranscriptionEngine` protocol, so the cloud engine drops in beside the local ones and nothing upstream is rewritten.

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
