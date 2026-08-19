# Worker API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /models` | bearer | model catalog, capabilities, pricing |
| `GET /usage` | bearer | today's spend plus 30 days of history |
| `POST /usage/reset` | bearer | clear the counters |
| `POST /transcribe` | bearer | audio bytes in, JSON out |

Auth is a single bearer token compared in constant time, set as the `AUTH_TOKEN` secret. The app keeps its copy in the login Keychain, not UserDefaults.

## Direct Workers AI API mode

The macOS app can skip this Worker and call Cloudflare's documented REST endpoint:

```text
POST https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{MODEL_ID}
Authorization: Bearer {WORKERS_AI_API_TOKEN}
Content-Type: application/json
```

Create Cloudflare's **Workers AI API Token** from **Workers AI > Use REST API**.
A manually-created token needs both **Workers AI - Read** and **Workers AI - Edit**.
The app queries `GET /client/v4/accounts` with that token, stores the first
visible account ID, and shows an account picker only if multiple accounts are
visible. New users therefore paste one value—the API token—rather than copying
an Account ID. Cloudflare returns its normal `{ success, errors, messages,
result }` envelope; the app presents an error message from `errors` when token,
account discovery, or inference is rejected.

The direct client mirrors the Worker registry's options and response parsing,
but not its audio input form. `env.AI.run` is an in-process binding, so
`src/core/models.js` can hand Nova-3 a `{ body: ReadableStream, contentType }`
object that no HTTP request can serialize. Each model's REST wire form is
therefore declared in `src/client/CloudflareDirectRequest.swift`, which is
authoritative for the direct path:

| Model | REST request body | Options |
|---|---|---|
| `nova-3` | raw audio bytes, `Content-Type: audio/wav` | query string, `keyterm` repeated per term |
| `whisper-turbo` | JSON, `audio` as base64 | JSON body |
| `whisper`, `whisper-tiny-en` | JSON, `audio` as a byte array | none |

Sending Nova-3 audio as JSON in any shape, including the binding's own object,
returns `400 AiError: Bad input: Error: required properties at '/audio' are
'body,contentType'`. Direct cleanup sends the same model IDs, system prompt,
messages, temperature, and token cap as `src/core/cleanup.js` and reads
`result.choices[0].message.content` (or `result.response`); chat models take the
same JSON over REST as over the binding. The Worker remains necessary only for
its usage counter and price/catalog endpoint, not transcription or cleanup.

## POST /transcribe

Raw audio as the request body (`audio/wav`, `audio/mpeg`, `audio/webm`), up to 25 MB.

| Query param | Default | Meaning |
|---|---|---|
| `model` | `nova-3` | `nova-3`, `whisper-turbo`, `whisper`, `whisper-tiny-en` |
| `language` | `auto` | ISO code, or `auto` to detect |
| `vocabulary` | none | comma separated terms to spell correctly (`initial_prompt` is an alias) |
| `cleanup` | off | `1` runs an LLM pass to strip filler words and fix punctuation |
| `cleanup_model` | `llama-8b` | `llama-8b`, `llama-3b`, `granite-micro`, `mistral-24b` |
| `instruction` | none | extra cleanup instruction, appended to the system prompt |
| `dictation` | off | `1` interprets spoken punctuation (nova-3 only) |

Response fields worth knowing:

| Field | Meaning |
|---|---|
| `text` | the transcript, cleaned if cleanup ran |
| `raw_transcript` | the pre-cleanup text, only when cleanup ran |
| `terms_applied` | which vocabulary terms actually reached the recognizer |
| `terms_skipped_reason` | why they did not, rather than failing silently |
| `language_mismatch` | set when the text came back in a script the language setting rules out |
| `audio_seconds`, `neurons` | what this request cost |

A language a model cannot serve returns 400 naming the supported set, rather than passing a provider 502 through.

## GET /usage

Today's audio seconds, neurons, free tier fraction, and billable dollars, plus per model breakdown and 30 days of history. Counts live in a Durable Object so concurrent dictations cannot lose an increment.

## Configuration

Server side defaults come from `vars` in `wrangler.jsonc`:

| Var | Purpose |
|---|---|
| `DEFAULT_MODEL` | model when the request names none |
| `DEFAULT_CLEANUP_MODEL` | LLM for the cleanup pass |
| `VOCABULARY` | terms applied to every request |
