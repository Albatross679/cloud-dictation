# cloud-dictation

Dictation for macOS that transcribes on Cloudflare Workers AI instead of on your Mac.

Hold a hotkey, speak, and the text lands in whatever app has focus.

Built on **[Starmel/OpenSuperWhisper](https://github.com/Starmel/OpenSuperWhisper)** (MIT)

## Why

Commercial dictation apps run $8 to $15 a month. Cloudflare gives 10,000 neurons per day free, on the free plan and the paid plan alike. For people who don't really use the free neurons, dictation would be a way to take advantage of that.

### Price

|Daily dictation|This, on Whisper base|This, on Nova-3|Wispr Flow|
|---|---|---|---|
|30 min|**$0**|$1.38/mo|$15/mo|
|1 hr|**$0**|$6.06/mo|$15/mo|

### Latency

|                   | This, on Whisper base | This, on Nova-3 | Wispr Flow         |
| ----------------- | --------------- | --------------- | ------------------------ |
| Latency, 9 s clip | ~1,250 ms       | **433 ms**      | <700 ms (vendor-claimed) |

## Install and configure - Direct API (recommended)

You do **not** need to deploy a Worker. The app can call Workers AI in your Cloudflare account directly.

1. **Download [OSW Cloud v0.1.0](https://github.com/Albatross679/cloud-dictation/releases/download/v0.1.0/OSW.Cloud.dmg)**, open the DMG, and drag `OSW Cloud.app` to `/Applications`. Or install with Homebrew: `brew install --cask albatross679/tap/osw-cloud`.
2. **Create a Cloudflare account** if you do not already have one.
3. In the Cloudflare dashboard, open **Workers AI** and select **Use REST API**. Choose **Create a Workers AI API Token**. This is Cloudflare's prefilled **Workers AI API Token** template. If making a custom token instead, grant both **Workers AI - Read** and **Workers AI - Edit**.
4. On a fresh launch, the app walks you through creating, testing, and saving a Direct API token in one window. You can also open **Settings > Models > Engine > Cloudflare**; paste **only the API token**. Use **Test Connection** to surface an invalid token or account before dictating.

### Settings worth knowing

- **Transcription > Language.** Nova-3 serves ten languages and errors on the rest. For anything else pick `whisper-turbo`.
- **Transcription > Vocabulary.** A comma separated term list, not prose. `R2, Kubernetes, Workers AI`. It measurably fixes proper nouns.
- **Cloudflare > Audio speed.** Choose `1`, `1.25`, `1.5`, `1.75`, `2`, `2.25`, `2.5`, `2.75`, or `3`; default `1` uploads the original recording unchanged. Higher speeds preserve pitch and cut billed audio minutes, but trade accuracy for cost - see the measured WER deltas in [asr-compression-cost](https://github.com/Albatross679/asr-compression-cost). 1.5 is recommended.

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

- [docs/models.md](docs/models.md) - which model to pick, languages, vocabulary, measured accuracy and cost
- [docs/api.md](docs/api.md) - worker endpoints and parameters
- [docs/building.md](docs/building.md) - build requirements, signing, packaging, reproducibility
