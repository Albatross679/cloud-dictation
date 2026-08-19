# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- The compression benchmark lives in its own repository, [Albatross679/asr-compression-cost](https://github.com/Albatross679/asr-compression-cost). It measures this worker over HTTP and is coupled to it by nothing but `CLOUD_DICTATION_WORKER` and `CLOUD_DICTATION_TOKEN`, so a change to the model registry or to `/transcribe` is a change that repository's runs will see. Its `docs/method.md` is authoritative for what it measures and how.
- The billing rates are declared in one place, `src/core/models.js`, and are served from `GET /models`. They were confirmed against live Cloudflare billing, so cost figures derived from them need no measurement. Nothing that consumes them may copy them.
- Cloudflare bills more Workers AI inferences than the worker issues: 0% excess at 10 requests, 4% at 50, 18% for 50 sent alongside 150 others across four models, measured 2026-08-12. `src/api/usage_counter.js` counts one inference per request served, so it reads low against the bill and the free daily allowance runs out sooner than it suggests. Every price is per audio minute and is unaffected; what the excess moves is how many billed minutes there are.
- Workers AI's REST API and the `env.AI` binding are not the same wire format, so the Direct API client cannot copy the Worker's input shape. Nova-3 over REST takes raw audio bytes with its options in the query string; every JSON form of `audio` is rejected with `required properties at '/audio' are 'body,contentType'`, including the binding's own `{ body: ReadableStream, contentType }` object, whose stream no request can serialize. `src/client/CloudflareDirectRequest.swift` is authoritative for the per-model REST wire form and `docs/api.md` tabulates it; `src/core/models.js` stays authoritative for the Worker path only. Chat models take identical JSON on both paths.
- `npm test` compiles and runs the Swift client tests with `swiftc` alone, so the request-encoding contract is checkable without Xcode, whisper.cpp, or Rust. A full app build still needs all of those (`docs/building.md`).
- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
