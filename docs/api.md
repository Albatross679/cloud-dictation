# Worker API

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness |
| `GET /models` | bearer | model catalog, capabilities, pricing |
| `GET /usage` | bearer | today's spend plus 30 days of history |
| `POST /usage/reset` | bearer | clear the counters |
| `POST /transcribe` | bearer | audio bytes in, JSON out |

Auth is a single bearer token compared in constant time, set as the `AUTH_TOKEN` secret. The app keeps its copy in the login Keychain, not UserDefaults.

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
