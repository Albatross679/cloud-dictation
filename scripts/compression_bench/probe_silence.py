"""Probe P2: is a model billed for the file, or only for the speech inside it?

The speech content is held fixed. One clip is built by concatenating corpus
utterances to exactly SILENCE_PROBE_SPEECH_S, then padded with 0, 2, 4, 8 and 16
seconds of silence, split evenly before and after. Each padding is its own
analytics window, so billed seconds can be read per model per padding.

The answer is the slope of billed seconds against padding seconds. A slope near
1 means the file is metered. A slope near 0 means only detected speech is
metered, which is what Nova-3 is known to do and what the Whisper family has
never been checked for.

This probe reads the same analytics P1 does and settles the same way: it polls
until every model's billed request count has reached at least what the padding's
window sent and the counts hold still across a further read, and records the
window as failed rather than as a result when a model stays billed for less than
it sent. The platform bills more inferences than the client sends, so each window
records its per-model excess, and a window whose excess is far above what the
platform produces is recorded for re-measurement instead. Consecutive windows are
held a real gap apart so one window's analytics lag cannot land inside the next
window's range.

Each padding's window is checkpointed to silence.windows.jsonl the moment it
closes and settles, so the probe can be run in short idle stretches: a re-run
skips the paddings already recorded and measures only what is left. A window
interrupted mid-flight is never checkpointed, and is discarded and re-measured
whole. --max-windows measures a bounded number of windows and then stops cleanly.

Writes runs/compression-bench/probes/silence.json, and only once every padding
has a window.

Building and verifying the padded clips is local ffmpeg work and always runs.
--dry-run stops before the network and synthesises the billing side.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

import config as cfg
import excess
import quiet_window as quiet
import window_log
from probe_billing import (floor_minute, hold_boundary_gap, iso, sent_counts, settled_window,
                           wait_for_boundary)

DURATION_TOLERANCE_S = 0.06

# Slopes of billed seconds against padding seconds. Anything between the two is
# reported as inconclusive rather than rounded to the nearer story.
FILE_METERED_ABOVE = 0.8
SPEECH_METERED_BELOW = 0.2


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build_speech(manifest, seconds, dst):
    """One clip of exactly `seconds` of speech, concatenated from corpus wavs."""
    picked = []
    total = 0.0
    for record in manifest:
        picked.append(record)
        total += record["duration_s"]
        if total >= seconds:
            break
    if total < seconds:
        sys.exit(f"corpus holds {total:.1f}s, needs {seconds:.1f}s")

    listing = dst.with_suffix(".txt")
    listing.write_text("".join(f"file '{cfg.RUN_DIR / r['path']}'\n" for r in picked))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(listing),
         "-t", f"{seconds:.3f}",
         "-ar", str(cfg.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )
    listing.unlink()
    return [r["utt_id"] for r in picked]


def build_padded(speech, padding, dst):
    """The speech clip with `padding` seconds of silence split evenly around it."""
    if padding == 0:
        shutil.copyfile(speech, dst)
        return
    half = padding / 2
    silence = f"anullsrc=r={cfg.SAMPLE_RATE}:cl=mono"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-t", f"{half:.3f}", "-i", silence,
         "-i", str(speech),
         "-f", "lavfi", "-t", f"{half:.3f}", "-i", silence,
         "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
         "-map", "[out]",
         "-ar", str(cfg.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )


def send_clip(session, path, model_key, base_url, token):
    params = {"model": model_key, "cleanup": "0"}
    if cfg.MODELS[model_key]["honors_language"]:
        params["language"] = cfg.LANGUAGE
    response = session.post(
        f"{base_url}/transcribe",
        params=params,
        data=path.read_bytes(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "audio/wav"},
        timeout=cfg.REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


def fake_window(models, repeats, speech_s, file_s):
    """Billing as it would look if Nova-3 metered speech and Whisper metered files.

    The split is the hypothesis under test, written out so the dry run exercises
    both branches of the classifier rather than one.
    """
    billed = {}
    for model_key in models:
        seconds = speech_s if model_key == "nova-3" else file_s
        billed[model_key] = {
            "requests": repeats,
            # Billed seconds are what the probe measures. Neurons are the
            # cross-check, and a dry run has no neuron rate to synthesise one.
            "neurons": 0.0,
            "audio_seconds": seconds * repeats,
            "inference_ms": 900.0 * repeats,
        }
    return billed


def slope(xs, ys):
    """Least squares slope of billed seconds against padding seconds."""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator


def verdict(value):
    if value is None:
        return "unknown"
    if value >= FILE_METERED_ABOVE:
        return "meters file duration"
    if value <= SPEECH_METERED_BELOW:
        return "meters detected speech"
    return "inconclusive"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="build and verify the clips, synthesise the billing side")
    mode.add_argument("--live", action="store_true",
                      help="send real requests to the worker and pay for them")
    parser.add_argument("--models", nargs="*", default=list(cfg.MODELS))
    parser.add_argument("--padding", nargs="*", type=float, default=cfg.SILENCE_PROBE_PADDING_S,
                        help="seconds of silence to add, one analytics window each")
    parser.add_argument("--repeats", type=int, default=cfg.SILENCE_PROBE_REPEATS,
                        help="requests per model per padding, so the window delta is readable")
    parser.add_argument("--speech", type=float, default=cfg.SILENCE_PROBE_SPEECH_S,
                        help="seconds of speech held fixed across every padding")
    parser.add_argument("--out", default=None, help="override the result path")
    parser.add_argument("--max-windows", type=int, default=None,
                        help="measure at most this many windows, then stop cleanly and "
                             "leave the rest for a later run")
    parser.add_argument("--window-offset", type=int, default=0,
                        help="how many quiet windows run before this probe, for the announcements")
    parser.add_argument("--window-total", type=int, default=None,
                        help="quiet windows in the whole sequence, for the announcements")
    args = parser.parse_args()

    unknown = [m for m in args.models if m not in cfg.MODELS]
    if unknown:
        raise SystemExit(f"unknown models: {', '.join(unknown)}")
    if args.max_windows is not None and args.max_windows < 1:
        raise SystemExit("--max-windows must be at least 1")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")
    if not cfg.MANIFEST.exists():
        sys.exit(f"missing {cfg.MANIFEST}; run prepare_corpus.py first")

    cfg.catalogue()

    with open(cfg.MANIFEST) as handle:
        manifest = [json.loads(line) for line in handle]

    cfg.PROBE_DIR.mkdir(parents=True, exist_ok=True)
    speech_path = cfg.PROBE_DIR / "speech.wav"
    sources = build_speech(manifest, args.speech, speech_path)
    speech_s = probe_duration(speech_path)
    print(f"speech clip {speech_path.name}: {speech_s:.3f}s from {len(sources)} utterances")
    if abs(speech_s - args.speech) > DURATION_TOLERANCE_S:
        sys.exit(f"speech clip is {speech_s:.3f}s, asked for {args.speech:.3f}s")

    clips = []
    for padding in args.padding:
        path = cfg.PROBE_DIR / f"pad-{padding:g}s.wav"
        build_padded(speech_path, padding, path)
        actual = probe_duration(path)
        expected = speech_s + padding
        if abs(actual - expected) > DURATION_TOLERANCE_S:
            sys.exit(f"{path.name} is {actual:.3f}s, expected {expected:.3f}s")
        clips.append({"padding_s": padding, "path": path, "duration_s": actual})
        print(f"  pad {padding:>4g}s -> {actual:7.3f}s  (speech {speech_s:.3f} + {padding:g}, "
              f"off by {actual - expected:+.3f}s)")

    audio_minutes = sum(c["duration_s"] for c in clips) * args.repeats / 60
    cost = sum(audio_minutes * cfg.usd_per_audio_minute(m) for m in args.models)
    print(f"\nP2 silence probe: {len(clips)} paddings x {len(args.models)} models x "
          f"{args.repeats} repeats")
    print(f"  {audio_minutes:.1f} audio minutes per model, ~${cost:.2f} total, "
          f"one settled analytics window per padding")

    out_path = cfg.RUN_DIR / args.out if args.out else cfg.SILENCE_PROBE_RESULT
    checkpoint = cfg.probe_windows_path(out_path, args.dry_run)
    # The shape is keyed on the speech length asked for, which the runner also
    # knows, rather than the measured length, which drifts by milliseconds
    # between ffmpeg runs. build_speech already holds the two within tolerance.
    shape = window_log.silence_shape(args.repeats, args.models, args.speech)
    log = window_log.load_windows(
        checkpoint, args.dry_run, shape, cfg.probe_windows_path(out_path, not args.dry_run))
    measured = log.measured
    planned = [
        {"key": window_log.silence_key(clip["padding_s"]),
         "label": f"{clip['padding_s']:g} s padding"}
        for clip in clips
    ]

    schedule = quiet.QuietSchedule(
        quiet.silence_windows(args.padding, args.repeats, args.models, speech_s),
        offset=args.window_offset,
        total=args.window_total,
        completed={i for i, plan in enumerate(planned) if plan["key"] in measured},
    )
    for record in measured.values():
        schedule.observe(record.get("settle_seconds_observed"))
    hold_seconds = quiet.boundary_hold_seconds(schedule.observed_settles)
    for line in window_log.resume_lines(planned, measured, checkpoint, args.max_windows):
        print(f"  {line}")
    for line in window_log.recovery_lines(
            planned, log.corrupt, checkpoint,
            f"probe_silence.py {'--dry-run' if args.dry_run else '--live'}"):
        print(f"  {line}")
    for line in schedule.plan_lines():
        print(f"  {line}")

    base_url = token = session = None
    if args.live:
        base_url = cfg.worker_url()
        token = cfg.auth_token()
        session = requests.Session()

    measured_now = 0
    for window_index, clip in enumerate(clips):
        plan = planned[window_index]
        if plan["key"] in measured:
            continue
        if args.max_windows is not None and measured_now >= args.max_windows:
            print(f"\nstopping after {measured_now} window"
                  f"{'s' if measured_now != 1 else ''}, as --max-windows asked")
            break
        print(f"\npadding {clip['padding_s']:g}s")
        schedule.open(window_index)
        start = floor_minute(datetime.now(timezone.utc))
        worker = {m: {"requests": 0, "audio_seconds": 0.0, "neurons": 0.0, "errors": 0}
                  for m in args.models}
        for model_key in args.models:
            for _ in range(args.repeats):
                if args.dry_run:
                    worker[model_key]["requests"] += 1
                    continue
                try:
                    body = send_clip(session, clip["path"], model_key, base_url, token)
                except Exception as err:
                    worker[model_key]["errors"] += 1
                    print(f"  {model_key}: {type(err).__name__}: {err}")
                    continue
                worker[model_key]["requests"] += 1
                worker[model_key]["audio_seconds"] += body.get("audio_seconds") or 0.0
                worker[model_key]["neurons"] += body.get("neurons") or 0.0
        end = floor_minute(datetime.now(timezone.utc)) + timedelta(minutes=1)
        wait_for_boundary(end, args.dry_run)

        settle = settled_window(
            session, start, end, args.models, sent_counts(worker),
            fake=(fake_window(args.models, args.repeats, speech_s, clip["duration_s"])
                  if args.dry_run else None),
        )
        rows = []
        for model_key in args.models:
            b = settle.totals.get(model_key, {"requests": 0, "neurons": 0.0, "audio_seconds": 0.0})
            sent = max(1, worker[model_key]["requests"])
            rows.append({
                "model": model_key,
                "requests_sent": worker[model_key]["requests"],
                "requests_billed": b["requests"],
                "billed_seconds_total": b["audio_seconds"],
                "billed_seconds_per_request": b["audio_seconds"] / sent,
                "billed_neurons_total": b["neurons"],
                "worker_seconds_per_request": worker[model_key]["audio_seconds"] / sent,
            })
            print(f"  {model_key:<17} billed {rows[-1]['billed_seconds_per_request']:7.2f} s/req  "
                  f"file {clip['duration_s']:.2f} s  speech {speech_s:.2f} s")
        # The window is complete only here, past the send and the settle, so this
        # is the first point at which anything about it may be written down.
        record = {
            "probe": "silence",
            "synthetic": args.dry_run,
            "window_key": plan["key"],
            "window_shape": shape,
            "padding_s": clip["padding_s"],
            "file_seconds": clip["duration_s"],
            "measured_at": window_log.now_iso(),
            "window_start": iso(start),
            "window_end": iso(end),
            "settled": settle.complete,
            "settle_seconds_observed": settle.lag_seconds,
            "settle_seconds_confirmed": settle.confirmed_seconds,
            "analytics_reads": settle.polls,
            # The excess is a measured property of the platform, so it travels with
            # the window it was measured in rather than being warned about once.
            "excess": settle.excess,
            "excess_rate": settle.excess_rate,
            "excess_implausible": settle.implausible,
            "models": rows,
        }
        if not settle.complete:
            record["shortfall"] = settle.missing
        window_log.append_window(checkpoint, record)
        # A failed window is written down so the operator can see it, and is left out
        # of the measured set so it is re-measured rather than scored. It cost the
        # same money as a good one, so it still counts against --max-windows.
        measured_now += 1
        if settle.usable:
            measured[plan["key"]] = record
            schedule.observe(settle.lag_seconds)
            hold_seconds = quiet.boundary_hold_seconds(schedule.observed_settles)
            schedule.mark_done(window_index)
        else:
            log.corrupt[plan["key"]] = record
        schedule.close(window_index, settle_seconds=settle.lag_seconds)
        if settle.usable:
            print(f"  billing excess this window: {excess.describe_rate(settle.excess)}")
            print(f"  checkpointed window {window_index + 1} of {len(planned)} to "
                  f"{checkpoint.name}")
        elif settle.complete:
            print(f"  recorded window {window_index + 1} of {len(planned)} for re-measurement "
                  f"in {checkpoint.name}: excess above the "
                  f"{cfg.EXCESS_IMPLAUSIBLE_ABOVE:.0%} the platform produces, "
                  f"{excess.describe(settle.implausible)}")
            print("  that reads as foreign traffic in the window, so no slope is fitted "
                  "through it")
        else:
            print(f"  recorded window {window_index + 1} of {len(planned)} as failed in "
                  f"{checkpoint.name}: {excess.describe(settle.missing)}")
            print("  it is not a measurement and will be re-measured; no slope is fitted "
                  "through it")
        hold_boundary_gap(end, hold_seconds, args.dry_run)

    if any(plan["key"] not in measured for plan in planned):
        print("\n" + window_log.progress_line(planned, measured, "P2 silence probe"))
        for line in window_log.recovery_lines(
                planned, log.corrupt, checkpoint,
                f"probe_silence.py {'--dry-run' if args.dry_run else '--live'}"):
            print(line)
        return 0

    # Every padding has a window, so the slope is fitted over the full set, in
    # padding order rather than the order the windows happened to be measured in.
    windows = [window_log.result_fields(measured[plan["key"]]) for plan in planned]

    summary = {}
    for model_key in args.models:
        xs, ys = [], []
        for window in windows:
            row = next(r for r in window["models"] if r["model"] == model_key)
            xs.append(window["padding_s"])
            ys.append(row["billed_seconds_per_request"])
        s = slope(xs, ys)
        summary[model_key] = {
            "billed_seconds_by_padding": dict(zip((f"{x:g}" for x in xs), ys)),
            "slope_seconds_per_padding_second": s,
            "billed_at_zero_padding": ys[0] if ys else None,
            "speech_seconds": speech_s,
            "verdict": verdict(s),
        }
        print(f"{model_key:<17} slope {s if s is None else round(s, 3)}  {verdict(s)}")

    span = window_log.measurement_span(w.get("measured_at") for w in windows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "probe": "P2 silence padding",
        "synthetic": args.dry_run,
        "speech_seconds": speech_s,
        "speech_sources": sources,
        "repeats": args.repeats,
        "measurement_span": span,
        "settle_seconds_observed": [w["settle_seconds_observed"] for w in windows],
        "windows": windows,
        "summary": summary,
    }, indent=2))
    print(f"\nwrote {out_path}")
    if span:
        print(f"windows measured between {span['first']} and {span['last']}, "
              f"spanning {span['days']:.2f} days")
        if span["days"] >= 1:
            print("that gap is a possible source of drift: the account's billing behaviour "
                  "may not have been the same across it")
    if args.dry_run:
        print("synthetic: the billing side assumes Nova-3 meters speech and Whisper meters files, "
              "which is the hypothesis this probe exists to test")
    return 0


if __name__ == "__main__":
    main()
