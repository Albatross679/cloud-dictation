"""Probe P2: is a model billed for the file, or only for the speech inside it?

The speech content is held fixed. One clip is built by concatenating corpus
utterances to exactly SILENCE_PROBE_SPEECH_S, then padded with 0, 2, 4, 8 and 16
seconds of silence, split evenly before and after. Each padding is its own
analytics window, so billed seconds can be read per model per padding.

The answer is the slope of billed seconds against padding seconds. A slope near
1 means the file is metered. A slope near 0 means only detected speech is
metered, which is what Nova-3 is known to do and what the Whisper family has
never been checked for.

Writes runs/compression-bench/probes/silence.json.

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
from probe_billing import floor_minute, iso, settled_window, wait_for_boundary

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
        rate = cfg.MODELS[model_key]["neurons_per_audio_minute"]
        billed[model_key] = {
            "requests": repeats,
            "neurons": seconds * repeats / 60 * rate,
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
    args = parser.parse_args()

    unknown = [m for m in args.models if m not in cfg.MODELS]
    if unknown:
        raise SystemExit(f"unknown models: {', '.join(unknown)}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")
    if not cfg.MANIFEST.exists():
        sys.exit(f"missing {cfg.MANIFEST}; run prepare_corpus.py first")

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
    cost = sum(
        audio_minutes * cfg.MODELS[m]["neurons_per_audio_minute"] for m in args.models
    ) * cfg.USD_PER_1000_NEURONS / 1000
    print(f"\nP2 silence probe: {len(clips)} paddings x {len(args.models)} models x "
          f"{args.repeats} repeats")
    print(f"  {audio_minutes:.1f} audio minutes per model, ~${cost:.2f} total, "
          f"one settled analytics window per padding")

    base_url = token = session = None
    if args.live:
        base_url = cfg.worker_url()
        token = cfg.auth_token()
        session = requests.Session()

    windows = []
    for clip in clips:
        print(f"\npadding {clip['padding_s']:g}s")
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

        billed, lag, polls = settled_window(
            session, start, end, args.models,
            fake=(fake_window(args.models, args.repeats, speech_s, clip["duration_s"])
                  if args.dry_run else None),
        )

        rows = []
        for model_key in args.models:
            b = billed.get(model_key, {"requests": 0, "neurons": 0.0, "audio_seconds": 0.0})
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
        windows.append({
            "padding_s": clip["padding_s"],
            "file_seconds": clip["duration_s"],
            "window_start": iso(start),
            "window_end": iso(end),
            "settle_seconds_observed": lag,
            "analytics_reads": polls,
            "models": rows,
        })

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

    out_path = cfg.RUN_DIR / args.out if args.out else cfg.SILENCE_PROBE_RESULT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "probe": "P2 silence padding",
        "synthetic": args.dry_run,
        "speech_seconds": speech_s,
        "speech_sources": sources,
        "repeats": args.repeats,
        "settle_seconds_observed": [w["settle_seconds_observed"] for w in windows],
        "windows": windows,
        "summary": summary,
    }, indent=2))
    print(f"\nwrote {out_path}")
    if args.dry_run:
        print("synthetic: the billing side assumes Nova-3 meters speech and Whisper meters files, "
              "which is the hypothesis this probe exists to test")


if __name__ == "__main__":
    main()
