"""Probe P1: do billed audio seconds fall proportionally with compression?

The per-minute rates in src/core/models.js are already confirmed against this
account's historical analytics, so establishing them is not this probe's job.
What is still unknown is whether the same speech, time-compressed by r, is
billed for 1/r of the seconds. That is the assumption the whole cost argument
rests on, and it is what this probe measures.

One window is a serialized batch of clips at one speed, read back from the
account-level aiInferenceAdaptiveGroups dataset. The delta from a single clip is
unreadable against a settling lag of minutes, so the batch is the unit of
measurement. Every clip is under the benchmark's 30 s cap, so the answer applies
to the short clips the benchmark actually runs on.

The measured quantity is totalAudioSeconds, which is the billed quantity itself.
totalNeurons is recorded alongside it as a cross-check.

The settle lag is measured, not assumed: each window is polled at minute
granularity until consecutive reads agree, and the observed lag is reported.

Writes runs/compression-bench/probes/billing.json.

--dry-run exercises the batch loop, the window arithmetic, the GraphQL payload,
the settle loop and the whole comparison against synthesised numbers, and opens
no socket.
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import requests

import config as cfg
import quiet_window as quiet

TOLERANCE = 0.02


def floor_minute(when):
    return when.replace(second=0, microsecond=0)


def iso(when):
    """Time as the analytics API wants it: UTC, second resolution, Z suffix."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def analytics_payload(start, end, models, account):
    """The exact GraphQL request body for one window."""
    return {
        "query": cfg.ANALYTICS_QUERY,
        "variables": {
            "account": account,
            "start": iso(start),
            "end": iso(end),
            "models": [cfg.MODELS[m]["model_id"] for m in models],
            "source": cfg.REQUEST_SOURCE,
        },
    }


def redacted_payload(models):
    """The same body with the account tag masked, for printing."""
    payload = analytics_payload(
        floor_minute(datetime.now(timezone.utc)),
        floor_minute(datetime.now(timezone.utc)) + timedelta(minutes=1),
        models, "<CLOUDFLARE_ACCOUNT_ID>",
    )
    payload["query"] = " ".join(payload["query"].split())
    return payload


def read_window(session, start, end, models):
    """Billed totals per model over [start, end), keyed by the worker's model key."""
    response = session.post(
        cfg.GRAPHQL_URL,
        json=analytics_payload(start, end, models, cfg.cloudflare_account()),
        headers={"Authorization": f"Bearer {cfg.cloudflare_token()}",
                 "Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise SystemExit(f"analytics query failed: {json.dumps(body['errors'])}")
    accounts = body["data"]["viewer"]["accounts"]
    if not accounts:
        raise SystemExit("analytics returned no account; check CLOUDFLARE_ACCOUNT_ID and the token scope")
    return fold_groups(accounts[0]["aiInferenceAdaptiveGroups"])


def fold_groups(groups):
    """Analytics rows folded onto worker model keys, unknown ids kept verbatim."""
    totals = {}
    for group in groups:
        model_id = group["dimensions"]["modelId"]
        key = cfg.MODEL_BY_ID.get(model_id, model_id)
        row = totals.setdefault(key, {"requests": 0, "audio_seconds": 0.0, "neurons": 0.0,
                                      "inference_ms": 0.0})
        row["requests"] += group["count"]
        row["audio_seconds"] += group["sum"]["totalAudioSeconds"]
        row["neurons"] += group["sum"]["totalNeurons"]
        row["inference_ms"] += group["sum"]["totalInferenceTimeMs"]
    return totals


def wait_for_boundary(end, dry_run):
    """Hold until a closed window's last minute is over.

    Minute buckets are inclusive of the minute a window ends in, so starting the
    next batch before that minute passes would count it in both windows.
    """
    if dry_run:
        return
    remaining = (end - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        print(f"  holding {remaining:.0f} s so the next window cannot share this minute")
        quiet.sleep_with_progress(remaining, "holding for the minute boundary")


def fingerprint(totals):
    """Comparable form of one window read, for deciding it has stopped moving."""
    return json.dumps(totals, sort_keys=True)


def settled_window(session, start, end, models, fake=None):
    """Poll a window until consecutive reads agree, and report how long that took.

    Returns the totals, the seconds between the window closing and the first of
    the agreeing reads, and how many reads it took.
    """
    if fake is not None:
        print("  dry run, not polling for the analytics to settle")
        return fake, None, 0

    closed = time.monotonic()
    deadline = closed + cfg.ANALYTICS_SETTLE_TIMEOUT_S
    previous = None
    agreements = 0
    polls = 0
    first_agreeing_lag = None
    while True:
        totals = read_window(session, start, end, models)
        polls += 1
        lag = time.monotonic() - closed
        current = fingerprint(totals)
        if previous is not None and current == previous and any(
            row["requests"] for row in totals.values()
        ):
            agreements += 1
            if first_agreeing_lag is None:
                first_agreeing_lag = lag - cfg.ANALYTICS_POLL_INTERVAL_S
            if agreements >= cfg.ANALYTICS_SETTLE_AGREEMENTS - 1:
                print(f"  settled after {first_agreeing_lag:.0f} s, {polls} reads")
                return totals, first_agreeing_lag, polls
        else:
            agreements = 0
            first_agreeing_lag = None
        previous = current
        if time.monotonic() >= deadline:
            print(f"  gave up waiting after {cfg.ANALYTICS_SETTLE_TIMEOUT_S} s, "
                  f"reporting the last read")
            return totals, None, polls
        billed = sum(row["requests"] for row in totals.values())
        print(f"    {lag:.0f} s after the window closed: {billed} requests visible, "
              f"reads agree {agreements} of {cfg.ANALYTICS_SETTLE_AGREEMENTS - 1} times")
        quiet.sleep_with_progress(cfg.ANALYTICS_POLL_INTERVAL_S,
                                  "still quiet, waiting for the analytics to settle")


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


def fake_clip(variant, model_key):
    """What the worker would report, from the same duration it would measure."""
    rate = cfg.MODELS[model_key]["neurons_per_audio_minute"]
    return {
        "audio_seconds": variant["duration_s"],
        "neurons": variant["duration_s"] / 60 * rate,
        "transcribe_ms": int(300 + variant["duration_s"] * 90),
    }


def fake_window(worker):
    """Billing as it would look if the file's own duration were billed."""
    return {
        key: {
            "requests": row["requests"],
            "audio_seconds": row["file_seconds"],
            "neurons": row["file_seconds"] / 60 * cfg.MODELS[key]["neurons_per_audio_minute"],
            "inference_ms": row["transcribe_ms"],
        }
        for key, row in worker.items()
    }


def run_batch(session, variants, models, base_url, token, dry_run):
    """One serialized pass over every clip for every model."""
    totals = {m: {"requests": 0, "neurons": 0.0, "audio_seconds": 0.0,
                  "transcribe_ms": 0.0, "file_seconds": 0.0, "errors": 0}
              for m in models}
    for model_key in models:
        for i, variant in enumerate(variants, 1):
            row = totals[model_key]
            try:
                if dry_run:
                    body = fake_clip(variant, model_key)
                else:
                    body = send_clip(session, cfg.RUN_DIR / variant["path"], model_key,
                                     base_url, token)
            except Exception as err:
                row["errors"] += 1
                print(f"  {model_key} {variant['utt_id']}: {type(err).__name__}: {err}")
                continue
            row["requests"] += 1
            row["neurons"] += body.get("neurons") or 0.0
            row["audio_seconds"] += body.get("audio_seconds") or 0.0
            row["transcribe_ms"] += body.get("transcribe_ms") or 0.0
            row["file_seconds"] += variant["duration_s"]
            if not dry_run and i % 50 == 0:
                print(f"  {model_key}: {i} / {len(variants)}")
    return totals


def compare(billed, worker):
    """One row per model: billed seconds against the seconds actually sent."""
    rows = []
    for model_key in sorted(set(billed) | set(worker)):
        b = billed.get(model_key, {"requests": 0, "audio_seconds": 0.0, "neurons": 0.0})
        w = worker.get(model_key, {"requests": 0, "audio_seconds": 0.0, "neurons": 0.0,
                                   "file_seconds": 0.0})
        seconds_ratio = (b["audio_seconds"] / w["file_seconds"]) if w.get("file_seconds") else None
        neurons_ratio = (b["neurons"] / w["neurons"]) if w.get("neurons") else None
        rows.append({
            "model": model_key,
            "requests_billed": b["requests"],
            "requests_sent": w.get("requests", 0),
            "audio_seconds_billed": b["audio_seconds"],
            "audio_seconds_sent": w.get("file_seconds", 0.0),
            "audio_seconds_ratio": seconds_ratio,
            "neurons_billed": b["neurons"],
            "neurons_worker": w.get("neurons", 0.0),
            "neurons_ratio": neurons_ratio,
            "billed_as_sent": seconds_ratio is not None and abs(seconds_ratio - 1.0) <= TOLERANCE,
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="synthesise the batch and the analytics read, open no socket")
    mode.add_argument("--live", action="store_true",
                      help="send real requests to the worker and pay for them")
    parser.add_argument("--models", nargs="*", default=list(cfg.MODELS),
                        help="subset of models to bill")
    parser.add_argument("--speeds", nargs="*", type=float, default=cfg.BILLING_PROBE_SPEEDS,
                        help="compression factors to compare, one window each")
    parser.add_argument("--clips", type=int, default=cfg.BILLING_PROBE_UTTERANCES,
                        help="clips per window")
    parser.add_argument("--replicates", type=int, default=cfg.BILLING_PROBE_REPLICATES)
    parser.add_argument("--out", default=None, help="override the result path")
    parser.add_argument("--window-offset", type=int, default=0,
                        help="how many quiet windows run before this probe, for the announcements")
    parser.add_argument("--window-total", type=int, default=None,
                        help="quiet windows in the whole sequence, for the announcements")
    args = parser.parse_args()

    unknown = [m for m in args.models if m not in cfg.MODELS]
    if unknown:
        raise SystemExit(f"unknown models: {', '.join(unknown)}")
    if cfg.BASELINE_SPEED not in args.speeds:
        raise SystemExit(f"--speeds must include the {cfg.BASELINE_SPEED:g}x baseline to compare against")
    if not cfg.VARIANTS.exists():
        raise SystemExit(f"missing {cfg.VARIANTS}; run compress.py first")

    with open(cfg.VARIANTS) as handle:
        pool = [json.loads(line) for line in handle]
    batches = {}
    for speed in args.speeds:
        picked = sorted(
            (v for v in pool if abs(v["speed"] - speed) < 1e-9),
            key=lambda v: v["utt_id"],
        )[: args.clips]
        if not picked:
            raise SystemExit(f"no variants at {speed:g}x in {cfg.VARIANTS}")
        batches[speed] = picked

    total_minutes = sum(sum(v["duration_s"] for v in b) for b in batches.values()) / 60
    cost = sum(
        total_minutes * cfg.MODELS[m]["neurons_per_audio_minute"] for m in args.models
    ) * cfg.USD_PER_1000_NEURONS / 1000 * args.replicates
    print(f"P1 billing probe: {args.clips} clips at "
          f"{', '.join(f'{s:g}x' for s in args.speeds)}, {args.replicates} replicates")
    print(f"  models {', '.join(args.models)}")
    print(f"  {total_minutes:.1f} audio minutes per model per replicate, ~${cost:.2f} total")
    print(f"  the account only has to be free of other dictation during each window: the query "
          f"filters on the four speech models and requestSource {cfg.REQUEST_SOURCE!r}, "
          f"so unrelated AI traffic is already excluded")

    schedule = quiet.QuietSchedule(
        quiet.billing_windows(args.speeds, args.replicates, args.clips, args.models, pool),
        offset=args.window_offset,
        total=args.window_total,
    )
    for line in schedule.plan_lines():
        print(f"  {line}")

    base_url = token = session = None
    if args.live:
        base_url = cfg.worker_url()
        token = cfg.auth_token()
        session = requests.Session()
    else:
        print("  dry run: the analytics request is built and never sent")
        print("  " + json.dumps(redacted_payload(args.models)))

    replicates = []
    window_index = 0
    for index in range(1, args.replicates + 1):
        windows = []
        for speed in args.speeds:
            print(f"\nreplicate {index} / {args.replicates}, {speed:g}x")
            schedule.open(window_index)
            variants = batches[speed]
            start = floor_minute(datetime.now(timezone.utc))
            worker = run_batch(session, variants, args.models, base_url, token, args.dry_run)
            end = floor_minute(datetime.now(timezone.utc)) + timedelta(minutes=1)
            print(f"  window {iso(start)} to {iso(end)}")
            wait_for_boundary(end, args.dry_run)

            billed, lag, polls = settled_window(
                session, start, end, args.models,
                fake=fake_window(worker) if args.dry_run else None,
            )
            schedule.observe(lag)
            schedule.close(window_index, settle_seconds=lag)
            window_index += 1
            rows = compare(billed, worker)
            windows.append({
                "speed": speed,
                "clips": len(variants),
                "window_start": iso(start),
                "window_end": iso(end),
                "settle_seconds_observed": lag,
                "analytics_reads": polls,
                "models": rows,
            })
            for row in rows:
                ratio = "n/a" if row["audio_seconds_ratio"] is None else f"{row['audio_seconds_ratio']:.4f}"
                print(f"  {row['model']:<17} billed {row['audio_seconds_billed']:>9.1f} s  "
                      f"sent {row['audio_seconds_sent']:>9.1f} s  ratio {ratio}")
        replicates.append({"replicate": index, "windows": windows})

    baseline = cfg.BASELINE_SPEED
    per_model = {}
    for model_key in args.models:
        def billed_at(window_speed, replicate):
            window = next(w for w in replicate["windows"] if w["speed"] == window_speed)
            return next(r for r in window["models"] if r["model"] == model_key)

        as_sent = [r["billed_as_sent"]
                   for rep in replicates for w in rep["windows"]
                   for r in w["models"] if r["model"] == model_key]
        proportionality = {}
        for speed in args.speeds:
            if speed == baseline:
                continue
            observed = []
            for replicate in replicates:
                base_seconds = billed_at(baseline, replicate)["audio_seconds_billed"]
                fast_seconds = billed_at(speed, replicate)["audio_seconds_billed"]
                if base_seconds:
                    observed.append(fast_seconds / base_seconds)
            expected = 1 / speed
            mean = sum(observed) / len(observed) if observed else None
            proportionality[f"{speed:g}"] = {
                "expected_billed_fraction": expected,
                "observed_billed_fraction": mean,
                "replicates": observed,
                "proportional": mean is not None and abs(mean - expected) <= TOLERANCE * expected,
            }
        settle = [w["settle_seconds_observed"] for rep in replicates for w in rep["windows"]
                  if w["settle_seconds_observed"] is not None]
        per_model[model_key] = {
            "billed_as_sent": bool(as_sent) and all(as_sent),
            "proportionality": proportionality,
        }
        for speed, row in proportionality.items():
            observed = "n/a" if row["observed_billed_fraction"] is None else f"{row['observed_billed_fraction']:.4f}"
            print(f"{model_key:<17} {speed}x billed {observed} of 1x, "
                  f"expected {row['expected_billed_fraction']:.4f}")

    settle_observed = [w["settle_seconds_observed"] for rep in replicates for w in rep["windows"]
                       if w["settle_seconds_observed"] is not None]
    out_path = cfg.RUN_DIR / args.out if args.out else cfg.BILLING_PROBE_RESULT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "probe": "P1 billed duration under compression",
        "synthetic": args.dry_run,
        "speeds": args.speeds,
        "clips_per_window": args.clips,
        "audio_minutes_per_replicate": total_minutes,
        "request_source_filter": cfg.REQUEST_SOURCE,
        "tolerance": TOLERANCE,
        "settle_seconds_observed": {
            "mean": sum(settle_observed) / len(settle_observed) if settle_observed else None,
            "max": max(settle_observed) if settle_observed else None,
            "windows": settle_observed,
        },
        "replicates": replicates,
        "summary": per_model,
    }, indent=2))
    print(f"\nwrote {out_path}")
    if settle_observed:
        print(f"observed settle lag: mean {sum(settle_observed) / len(settle_observed):.0f} s, "
              f"max {max(settle_observed):.0f} s")
    if args.dry_run:
        print("synthetic: the billed side was generated from the durations that were sent, so "
              "agreement here proves the arithmetic runs, not that billing is proportional")


if __name__ == "__main__":
    main()
