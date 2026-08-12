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

## Where the rates come from

The per-minute price, the free minutes a day and the `@cf/...` id billing analytics reports each model under are read from the deployed worker's `GET /models` catalogue, once at the start of every run, dry or live. Nothing about billing is stored in this harness, so its numbers cannot drift away from the app that serves them. The endpoint is free and sends no audio.

The response is cached to `runs/compression-bench/models-catalogue.json` with the time it was fetched, so a run still works while the worker is unreachable. A run served from that cache prints `STALE:` with the cache's timestamp and the reason the worker was not reached, and the report carries the same warning as a banner. Every report footer states when the catalogue it used was fetched.

There are no built-in rates. When the worker cannot be reached and there is no cache, the run stops and names the two variables to set.

## The no-inference boundary

Every stage except the live paths runs offline and free. The boundary is explicit in the code: `run.py`, `probe_billing.py` and `probe_silence.py` each require either `--dry-run` or `--live` and refuse to guess. Only `--live` reaches the worker, and only `--live` spends money.

`--dry-run` is not a stub. It exercises the same loops, the same resume log, the same window arithmetic, the same GraphQL payload construction and the same scoring and reporting path, substituting synthesised responses at the network boundary. A dry run is how the pipeline is proven before any inference is bought, and every artifact it produces is stamped `synthetic: true` so a dry run can never be mistaken for a measurement. The report renders a banner saying so.

The two modes also write different files. Stages 3, 4 and 5 each take `--dry-run` or `--live`, and each mode owns its own resume log, results file and report:

| Mode | Stage 3 writes | Stage 4 writes | Stage 5 writes | Each probe checkpoints to |
| --- | --- | --- | --- | --- |
| `--live` | `responses.jsonl` | `results.json` | `report.html` | `probes/<probe>.windows.jsonl` |
| `--dry-run` | `responses.dry-run.jsonl` | `results.dry-run.json` | `report.dry-run.html` | `probes/<probe>.windows.dry-run.jsonl` |

Separate files are what makes the bad state impossible rather than merely detectable: a live run cannot resume over a dry run's cells, and no results file can be a blend of measured and invented responses. On top of that, every stage checks the `synthetic` flag on the records it opens against its own mode, so a log that was copied, renamed, or left over from before the split stops the run with a message naming the file and the fix.

## The one command

Everything, in order, from one command. From `scripts/compression_bench/`:

    ../../runs/compression-bench/.venv/bin/python run_all.py --live

It prints the whole plan first: every stage, the request count, the estimated cost, and how long you must not dictate. Nothing is sent until you type `RUN LIVE` at the prompt. The confirmation is read from a terminal, so a redirected or empty stdin refuses rather than starting the run. Add `--plan-only` to see the plan and stop, and `--dry-run` in place of `--live` to walk the whole sequence for free.

The phase order is the point: the main grid, scoring and the report run first, because none of them constrain you, and the two probes run last, because they are the only stages that do. A stage whose output is already complete says so and is skipped, so an interrupted sequence is resumed by re-running the identical command. The first failing stage stops the sequence and the later stages do not run.

`--grid-only` runs the grid, scoring and the report, with no quiet windows at all. `--probes-only` runs the two probes on their own, for doing them later or overnight.

`--max-windows N` measures at most N more probe windows and then stops cleanly, which is how the probes are run in short idle stretches instead of one long sitting:

    run_all.py --live --probes-only --max-windows 2

The budget is spent in running order: P1 takes what it needs first, P2 gets what is left, and a probe with windows left but no budget is reported as left for a later run rather than started. Every window measured is checkpointed, so the next invocation of the same command picks up where this one stopped. The plan is sized from the windows that actually remain, and quotes both the quiet this run costs and the quiet still left to finish every probe.

## When you must not dictate

Only inside a probe's measurement windows, and the run tells you when each one starts and ends.

`run.py`, the main grid, records cost and duration from each response individually. No other traffic on the account can affect it, so dictate freely for the whole of that stage. The probes are different: they read account-level analytics filtered to the four speech models and the Workers-binding request source, which is the exact path the dictation app's own requests take. Anything dictated inside a measurement window is counted into that window and silently corrupts it.

Today's configuration opens 11 measurement windows: 3 replicates at 2 speeds for P1, and 5 paddings for P2. Each window costs its send time, then the longer of its settle and the gap held before the next window may open. The settle polls once a minute until every model's billed request count has reached at least what that window sent and the counts hold still across a further read, capped at 30 minutes; the gap is at least 3 minutes, measured from the window's own end. The runner prints the range, and each window's own share, before you commit to anything; once real settle times have been measured the estimate for the windows still to come is recomputed from them instead of the assumed bracket.

You do not have to sit through that in one block. Windows are checkpointed as they complete, so the 11 are split across as many sittings as you like, and the estimate counts only the windows still to run.

Each window is announced by a full-width block that says `DO NOT DICTATE` and which window this is out of how many. When the window has closed and settled, an equally distinct block says `SAFE TO DICTATE`. During the hold and the settle a line keeps counting down, so a poll that only fires once a minute never looks like a frozen run.

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

Stage 5 runs on its own as well as at the end. The report's cost half is arithmetic on the catalogue's published rates, which have been confirmed against real billing, so `report.py` renders it from the catalogue alone with no results file present. Every accuracy section then renders an empty state that names the stage that fills it, and no accuracy figure, table or number is drawn. That is the report's standing rule: a section either has data behind it or says it has none. The catalogue is still required, because there are no built-in rates to render a cost from.

Everything is written under `runs/compression-bench/`, which is gitignored. Only `scripts/compression_bench/` is committed.

Stages 1 and 2 are idempotent: an existing corpus is not re-downloaded and an existing variant is not re-encoded.

## Resume

Each mode's response log is its resume log. `run.py` reads the log its mode owns, skips every cell already recorded as successful there, and reports what is left before it starts. Interrupt it at any point and re-run the identical command: it continues where it stopped, never repeats a completed cell, and never pays for one twice. Failed cells are recorded too, and are retried rather than skipped.

A run only ever resumes over its own kind of response. A live run that finds synthetic records, or a dry run that finds real ones, prints what the file holds and what to rename it to, and exits without running a cell. A log holding both kinds cannot be resumed or scored by either mode and has to be moved aside.

A `responses.jsonl` written before the split holds a dry run's records. Rename it to `responses.dry-run.jsonl`, along with `results.json` and `report.html` to their `dry-run` names, and both modes work from there.

### The probes resume per measurement window

A probe's unit of resume is one measurement window: a batch sent, a settle waited out, an analytics delta read. Each probe appends the whole of a window's result to its own checkpoint log the moment that window closes and settles, before the next one opens. Interrupt a probe at any point and re-run the identical command: it names the windows it is skipping and the ones it still has to measure, and measures only those. A kill loses at most the window in flight, never a completed one.

- **A window in flight is discarded, not resumed.** Its measurement depends on an uninterrupted send followed by a clean settle, so half of one is not salvageable. Nothing is written until the settle returns, and a line torn by a kill mid-write fails to decode and is dropped on the next read. Either way that window is re-measured whole, and both the code and the output say so.
- **The analysis runs only when every window exists.** A probe that is short of windows reports `4 of 6 windows measured, 2 remaining` and stops without writing a result. No number is ever computed from partial data.
- **Pairing is by key, not by order.** P1 keys a window on its replicate and its speed together, so the 1x against 3x comparison inside a replicate is always between that replicate's own two windows, however the run was split up or in whatever order the windows were measured. P2 keys on the padding.
- **Each window records when it was measured.** The result file carries `measured_at` per window and a `measurement_span` over all of them, so a run split across days shows the gap, which is a possible source of drift.
- **A checkpoint from one mode never satisfies the other.** Each mode owns its own checkpoint file, and every record is checked against the mode before anything is skipped, the same guard the grid's response log uses.
- **A checkpoint from a differently shaped batch is refused.** Each record carries the clip count, the models and the speech length its window sent. Change those and the run stops rather than comparing windows that measured different things.
- **A checkpoint the analytics never fully accounted for is refused.** A window counts only when every model's billed request count has reached at least what it sent, and the excess above that is within what the platform itself produces. One that fails either is held out of the measured set, named with the reason and its per-model counts, and re-measured.

`--max-windows N` on either probe measures at most N more windows and then stops cleanly, leaving the rest for a later run.

## The probes

Cost in the report is arithmetic on the published rates. Two probes are what keep that arithmetic from resting on an assumption. Both read Cloudflare's account-level `aiInferenceAdaptiveGroups` dataset over the minute range of a batch and compare it against what the worker reported.

**P1, `probe_billing.py`.** Do billed audio seconds fall proportionally when the same speech is compressed? A window is a serialized batch of clips at one speed; the delta from a single clip is unreadable against a settling lag of minutes, so the batch is the unit of measurement. Three replicates, at 1x and 3x, with the ratio between them as the result. Every clip is under the 30 s cap, so the answer applies to the clips the benchmark actually runs on.

**P2, `probe_silence.py`.** Is silence billed? One clip of fixed speech content, padded with 0, 2, 4, 8 and 16 s of silence split evenly before and after, one analytics window per padding. The answer is the slope of billed seconds against padding seconds: near 1 means the file is metered, near 0 means only detected speech is. Nova-3 is known to meter detected speech; the Whisper family has never been checked. Building and verifying the padded clips is local ffmpeg work and runs in both modes.

Three facts about the analytics shape both probes:

- `totalAudioSeconds` is available, so billed duration is read directly rather than inferred from neurons. `totalNeurons` is kept as a cross-check.
- The settle is on the whole bill having arrived, and the lag is measured rather than assumed. See below.
- The platform bills more inferences than the client sends. The excess is measured per window and compared across speeds. See below.
- Windows are isolated by three filters together: the four speech model ids, `requestSource` = `unknown` (what a Worker AI binding reports, as against `rest api` for direct calls), and the minute range. The `tag` dimension is empty on every record in this account and cannot be used. Only dictation has to stop during a window, not all account AI traffic.

### The settle, and why it is on arrival

A probe knows exactly how many requests each model sent in a window. That count is the floor the settle waits for: each window is polled at minute granularity until every model's billed request count has reached at least what that window sent and the counts then hold still across a further read, so a count read while the bill is still landing is not mistaken for the end.

Stability alone is not a substitute for the floor, and treating it as one is what a live P1 run of 2026-08-12 proved. Account analytics stay unchanged for a whole poll interval while records are still arriving, so a read showing 3 of one model's 50 requests is exactly as stable as a finished one. Windows were checkpointed off two agreeing reads while the bill was still arriving: 46 and 43 of 50, then 37 and 3 of 50. The audio-seconds ratio is the probe's entire output and every one of those ratios is meaningless. One would have been published as nova-3 billing 1.57x what was sent at 3x.

- **A count below what was sent** means this window's own traffic had not all arrived. The window keeps polling, and reaching the 30 minute cap still short records it as failed, with its per-model undercount, and leaves it out of the measured set so it is re-measured rather than scored.
- **A count above what was sent** is the platform, not a fault. Equality is unreachable, so it is not the condition; see the next section.

The reported settle is what was observed: seconds from the window's own end to the first read in which the whole bill had arrived, alongside the read that confirmed it. No inference about when the data *became* stable is reported as though it were a measurement.

### The platform bills more inferences than the client sends

Measured on 2026-08-12 against the deployed worker: 10 requests billed 10, 50 billed 52, and 50 sent alongside 150 others across four models billed 59. Every request returned 200, the worker makes exactly one `env.AI.run` call per request, and nothing retries on this side. The excess is real, reproducible, and grows with load. That the platform retries internally and bills each attempt is a hypothesis about Cloudflare's internals; the billing itself is the measurement.

- **The excess is recorded, not warned about.** Every window carries its per-model billed minus sent, and the rate, in its checkpoint and in the result file. It is a measured property of the platform.
- **A large excess is treated as foreign traffic.** A model billed for more than `EXCESS_IMPLAUSIBLE_ABOVE` (25%) above what it sent is read as another source's requests inside the window, which is what the quiet-window rule exists to prevent. That window is refused, named, and re-measured rather than averaged in. The threshold sits above the worst the platform itself produced, which was 18%.
- **The rates are compared across speeds before any ratio is published.** P1's whole output is billed seconds at one speed as a share of another, and billed requests the client never issued carry billed seconds with them, so an excess rate that differs between 1x and 3x moves that ratio by about the difference. The budget is `EXCESS_SPREAD_BUDGET` (2%), the same tolerance the proportionality verdict is held to. Above it, the probe and the report both state that the ratio is not trustworthy and no ratio is published.

For the product, the consequence is that the worker's own usage counter counts one inference per request served and the bill holds more than that, so the counter reads low and the free daily allowance runs out sooner than it suggests. Every price is per audio minute and is unaffected; what the excess moves is how many billed minutes there are.

### The gap between windows

Consecutive windows are held apart, measured from the closing window's end, so one window's analytics lag tail cannot land inside the next window's range. The floor is 3 minutes, comfortably above the worst lag the live run observed at 129 s; once settles have been measured the gap is derived from the worst of them and never drops below the floor. The same live run opened its windows a minute apart, which is the direct explanation for a window billing 59 of the 50 requests it sent right after a neighbouring batch.

### Windows already on disk whose counts never matched

A checkpoint counts as a measurement only when every model's billed count reached what its window sent and the excess above that is the platform's own, and both are read from the counts in the record itself, so a window written before these checks existed is classified the same way. A window that fails either is refused, named with the reason and its per-model counts, and reported with the command that re-measures it. Re-running the identical probe command measures exactly those windows and the new record supersedes the corrupt one under the same key; every window whose counts did match is still skipped. No result is written while any window is corrupt, so nothing is scored from one.

The per-minute rates the worker publishes are already confirmed against this account's historical analytics, so neither probe needs to establish them.

## Credentials

| Variable | Used by | For |
| --- | --- | --- |
| `CLOUD_DICTATION_WORKER` | every run | the deployed worker's base URL, for the model catalogue and for `/transcribe` on live paths |
| `CLOUD_DICTATION_TOKEN` | every run | worker auth, read from the environment only |
| `CLOUDFLARE_ACCOUNT_ID` | probes | the account the analytics query is scoped to |
| `CLOUDFLARE_API_TOKEN` | probes | reading account analytics |

Read from the environment, never hardcoded, never printed. A dry run needs the two worker variables as well, because it costs the same models from the same catalogue a live run does; without them it falls back to the cached catalogue and says so. The Cloudflare token is account-owned, so `GET /client/v4/user/tokens/verify` returns `Invalid API Token` for it and is not a valid preflight check; issuing the real GraphQL query is.

## The real run

When the captain authorises inference, this is the command:

    CLOUD_DICTATION_WORKER=https://<worker>.workers.dev \
    CLOUD_DICTATION_TOKEN=<token> \
    ../../runs/compression-bench/.venv/bin/python run_all.py --live

That drives the stages below in order. Any of them still runs on its own, which is what `run_all.py` calls and what to reach for when only one stage needs redoing:

    ../../runs/compression-bench/.venv/bin/python run.py --live
    ../../runs/compression-bench/.venv/bin/python score.py --live
    ../../runs/compression-bench/.venv/bin/python report.py --live

The probes are separately authorised and separately paid:

    ../../runs/compression-bench/.venv/bin/python probe_billing.py --live
    ../../runs/compression-bench/.venv/bin/python probe_silence.py --live

`run.py --live` states where its rates came from, then prints the request count, the billed minutes and the estimated cost before the first request. A probe run on its own numbers its own windows; `run_all.py` passes `--window-offset` and `--window-total` so the announcements count across both probes instead of restarting, and `--max-windows` when the run is bounded.

## Tests

The mode split and the grid's resume log are covered by `test_response_log.py`. The quiet-window banners, the quiet-time estimate, the stage selection and the typed confirmation are covered by `test_run_plan.py`. The probes' per-window checkpoints, their resume, the window budget and the runner's shrinking plan are covered by `test_window_log.py`. The settle rule, the excess it records and its comparison across speeds, the gap between windows and the recovery from the windows the live run corrupted are covered by `test_probe_settle.py`, over the counts that run actually wrote. The model catalogue and the cost arithmetic built on it are covered by `test_catalogue.py`. The report's two halves are covered by `test_report.py`: that every cost figure is the catalogue's own arithmetic, and that no accuracy section renders a number, a chart or a figure label without a results file behind it. All six use the standard library's `unittest`, open no socket, and need no dependency beyond `requirements.txt`. From `scripts/compression_bench/`:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_response_log test_run_plan test_window_log test_probe_settle test_catalogue test_report -v
