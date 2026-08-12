# Compression versus accuracy benchmark

Cloudflare bills speech to text per audio minute, so time-compressing a recording by a factor r cuts that bill by exactly 1 - 1/r. That half is arithmetic and needs no experiment. What compression costs in accuracy is the unknown, and this harness measures it.

## The settled scope

| | |
| --- | --- |
| Corpus | LibriSpeech test-clean, 350 utterances, seed 20260811, capped at 30 s each |
| Models | nova-3, whisper-turbo, whisper, whisper-tiny-en |
| Speeds | 1.0, 1.5, 2.0, 2.5, 3.0, pitch preserving via ffmpeg atempo |
| Codec | held constant at 16 kHz mono PCM, so duration is the only thing that changes |
| Measured | WER after the Whisper English normalizer, change against each model's own 1x, the substitution/deletion/insertion split, catastrophic rate, latency |
| Computed | cost, from the published per-minute rates, never measured per cell |
| Acceptance | a speed passes when the error increase is at most 1.0 points, the upper bound of its 95% paired-bootstrap interval is under 2.0, and the catastrophic rate rises by at most 0.5 points |

Everything above lives in `config.py`. Change it there, not in the stages.

## The no-inference boundary

Every stage except the live paths runs offline and free. The boundary is explicit in the code: `run.py`, `probe_billing.py` and `probe_silence.py` each require either `--dry-run` or `--live` and refuse to guess. Only `--live` reaches the worker, and only `--live` spends money.

`--dry-run` is not a stub. It exercises the same loops, the same resume log, the same window arithmetic, the same GraphQL payload construction and the same scoring and reporting path, substituting synthesised responses at the network boundary. A dry run is how the pipeline is proven before any inference is bought, and every artifact it produces is stamped `synthetic: true` so a dry run can never be mistaken for a measurement. The report renders a banner saying so.

The two modes also write different files. Stages 3, 4 and 5 each take `--dry-run` or `--live`, and each mode owns its own resume log, results file and report:

| Mode | Stage 3 writes | Stage 4 writes | Stage 5 writes |
| --- | --- | --- | --- |
| `--live` | `responses.jsonl` | `results.json` | `report.html` |
| `--dry-run` | `responses.dry-run.jsonl` | `results.dry-run.json` | `report.dry-run.html` |

Separate files are what makes the bad state impossible rather than merely detectable: a live run cannot resume over a dry run's cells, and no results file can be a blend of measured and invented responses. On top of that, every stage checks the `synthetic` flag on the records it opens against its own mode, so a log that was copied, renamed, or left over from before the split stops the run with a message naming the file and the fix.

## Running it in order

Set up once:

    python3 -m venv runs/compression-bench/.venv
    runs/compression-bench/.venv/bin/pip install -r scripts/compression_bench/requirements.txt

Then, from `scripts/compression_bench/`, with `PY=../../runs/compression-bench/.venv/bin/python`:

| Stage | Command | Writes |
| --- | --- | --- |
| 1 | `$PY prepare_corpus.py` | `corpus/manifest.jsonl`, one record per utterance with its reference, duration, word count and baseline rate. Downloads and extracts test-clean (346 MB) on first run only. |
| 2 | `$PY compress.py` | `audio/speed-*/`, every variant, and `audio/variants.jsonl`. Verifies each file's measured duration against source over r and fails loudly if one drifts. |
| 3 | `$PY run.py --dry-run` | `responses.dry-run.jsonl`, one line per cell. Append-only and resumable. |
| 4 | `$PY score.py --dry-run` | `results.dry-run.json`: the grid, the paired-bootstrap intervals, the rate curve, the failure gallery. |
| 5 | `$PY report.py --dry-run` | `report.dry-run.html`. |

Swap `--dry-run` for `--live` in stages 3 to 5 to work on the measured run instead; the file names lose the `dry-run` infix.

Everything is written under `runs/compression-bench/`, which is gitignored. Only `scripts/compression_bench/` is committed.

Stages 1 and 2 are idempotent: an existing corpus is not re-downloaded and an existing variant is not re-encoded.

## Resume

Each mode's response log is its resume log. `run.py` reads the log its mode owns, skips every cell already recorded as successful there, and reports what is left before it starts. Interrupt it at any point and re-run the identical command: it continues where it stopped, never repeats a completed cell, and never pays for one twice. Failed cells are recorded too, and are retried rather than skipped.

A run only ever resumes over its own kind of response. A live run that finds synthetic records, or a dry run that finds real ones, prints what the file holds and what to rename it to, and exits without running a cell. A log holding both kinds cannot be resumed or scored by either mode and has to be moved aside.

A `responses.jsonl` written before the split holds a dry run's records. Rename it to `responses.dry-run.jsonl`, along with `results.json` and `report.html` to their `dry-run` names, and both modes work from there.

## The probes

Cost in the report is arithmetic on the published rates. Two probes are what keep that arithmetic from resting on an assumption. Both read Cloudflare's account-level `aiInferenceAdaptiveGroups` dataset over the minute range of a batch and compare it against what the worker reported.

**P1, `probe_billing.py`.** Do billed audio seconds fall proportionally when the same speech is compressed? A window is a serialized batch of clips at one speed; the delta from a single clip is unreadable against a settling lag of minutes, so the batch is the unit of measurement. Three replicates, at 1x and 3x, with the ratio between them as the result. Every clip is under the 30 s cap, so the answer applies to the clips the benchmark actually runs on.

**P2, `probe_silence.py`.** Is silence billed? One clip of fixed speech content, padded with 0, 2, 4, 8 and 16 s of silence split evenly before and after, one analytics window per padding. The answer is the slope of billed seconds against padding seconds: near 1 means the file is metered, near 0 means only detected speech is. Nova-3 is known to meter detected speech; the Whisper family has never been checked. Building and verifying the padded clips is local ffmpeg work and runs in both modes.

Three facts about the analytics shape both probes:

- `totalAudioSeconds` is available, so billed duration is read directly rather than inferred from neurons. `totalNeurons` is kept as a cross-check.
- The settle lag is measured, not assumed. Each window is polled at minute granularity until consecutive reads agree, and the observed lag is reported.
- Windows are isolated by three filters together: the four speech model ids, `requestSource` = `unknown` (what a Worker AI binding reports, as against `rest api` for direct calls), and the minute range. The `tag` dimension is empty on every record in this account and cannot be used. Only dictation has to stop during a window, not all account AI traffic.

The per-minute rates in `src/core/models.js` are already confirmed against this account's historical analytics, so neither probe needs to establish them.

## Credentials

| Variable | Used by | For |
| --- | --- | --- |
| `CLOUD_DICTATION_WORKER` | live paths only | the deployed worker's base URL |
| `CLOUD_DICTATION_TOKEN` | live paths only | worker auth, falling back to `.auth-token.local` |
| `CLOUDFLARE_ACCOUNT_ID` | probes | the account the analytics query is scoped to |
| `CLOUDFLARE_API_TOKEN` | probes | reading account analytics |

Read from the environment, never hardcoded, never printed. The Cloudflare token is account-owned, so `GET /client/v4/user/tokens/verify` returns `Invalid API Token` for it and is not a valid preflight check; issuing the real GraphQL query is.

## The real run

When the captain authorises inference, this is the command:

    CLOUD_DICTATION_WORKER=https://<worker>.workers.dev \
    ../../runs/compression-bench/.venv/bin/python run.py --live

It prints the request count, the billed minutes and the estimated cost before the first request. Then `score.py --live` and `report.py --live`, which read and write the live set of files.

The probes are separately authorised and separately paid:

    ../../runs/compression-bench/.venv/bin/python probe_billing.py --live
    ../../runs/compression-bench/.venv/bin/python probe_silence.py --live

## Tests

The mode split and the resume log are covered by `test_response_log.py`, which uses the standard library's `unittest` and needs no dependency beyond `requirements.txt`. From `scripts/compression_bench/`:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_response_log -v
