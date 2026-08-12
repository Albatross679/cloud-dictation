"""Probe P1: do billed audio seconds fall proportionally with compression?

The per-minute rates come from the worker's own catalogue at run time and are
already confirmed against this account's historical analytics, so establishing
them is not this probe's job. What is still unknown is whether the same speech, time-compressed by r, is
billed for 1/r of the seconds. That is the assumption the whole cost argument
rests on, and it is what this probe measures.

One window is a serialized batch of clips at one speed, read back from the
account-level aiInferenceAdaptiveGroups dataset. The delta from a single clip is
unreadable against a settling lag of minutes, so the batch is the unit of
measurement. Every clip is under the benchmark's 30 s cap, so the answer applies
to the short clips the benchmark actually runs on.

The measured quantity is totalAudioSeconds, which is the billed quantity itself.
totalNeurons is recorded alongside it as a cross-check.

The settle is on completeness: the probe knows how many requests it sent, and
polls at minute granularity until every model's billed request count equals that,
then over one further read to confirm. A window that reaches the timeout with the
counts still disagreeing is recorded as failed with its per-model shortfall and is
re-measured, because a count below what was sent means this window's traffic had
not all arrived and a count above it means another window's traffic was counted.
The lag reported is the observed one: seconds from the window's own end to the
first read that accounted for every request.

Consecutive windows are separated by a real gap, held from the closing window's
end, so one window's analytics lag tail cannot land inside the next window's range.

Each window is checkpointed to billing.windows.jsonl the moment it closes and
settles, so the probe can be run in short idle stretches: a re-run skips the
windows already recorded and measures only what is left. A window interrupted
mid-flight is never checkpointed, and is discarded and re-measured whole.
--max-windows measures a bounded number of windows and then stops cleanly.

Writes runs/compression-bench/probes/billing.json, and only once every window
exists.

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
import window_log

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
            "models": [cfg.model_id(m) for m in models],
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
        key = cfg.model_key_for_id(model_id)
        row = totals.setdefault(key, {"requests": 0, "audio_seconds": 0.0, "neurons": 0.0,
                                      "inference_ms": 0.0})
        row["requests"] += group["count"]
        row["audio_seconds"] += group["sum"]["totalAudioSeconds"]
        row["neurons"] += group["sum"]["totalNeurons"]
        row["inference_ms"] += group["sum"]["totalInferenceTimeMs"]
    return totals


def wait_for_boundary(end, dry_run):
    """Hold until a closed window's last minute is over.

    Minute buckets are inclusive of the minute a window ends in, so reading the
    window back before that minute passes reads a bucket that is still open.
    """
    if dry_run:
        return
    remaining = (end - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        print(f"  holding {remaining:.0f} s for the window's last minute to close")
        quiet.sleep_with_progress(remaining, "holding for the minute boundary")


def hold_boundary_gap(end, hold_seconds, dry_run):
    """Hold after a window is recorded so the next one cannot share its lag tail.

    Analytics for one batch keep arriving for minutes after the batch is sent. The
    gap is measured from the window's own end, so a window whose settle already ran
    past the hold waits no longer.
    """
    if dry_run:
        return
    remaining = hold_seconds - (datetime.now(timezone.utc) - end).total_seconds()
    if remaining > 0:
        print(f"  holding {remaining:.0f} s more so the next window cannot share this "
              f"window's analytics lag")
        quiet.sleep_with_progress(remaining, "holding the gap before the next window")


def sent_counts(worker):
    """Requests per model this window actually sent, from the worker's own tally."""
    return {model_key: row["requests"] for model_key, row in worker.items()}


def billed_counts(totals, models):
    """Requests per model the analytics account for, over the models asked about."""
    return {model_key: totals.get(model_key, {}).get("requests", 0) for model_key in models}


def complete_read(totals, expected):
    """True when the analytics account for exactly the requests the window sent.

    This is the invariant the probe can check, and stability cannot substitute for
    it: analytics stay unchanged for a whole poll interval while data is still
    arriving, and a read where one model has 3 of its 50 is as stable as a finished
    one. A count above what was sent is as disqualifying as one below, because it
    means another window's traffic is in this window's range.
    """
    billed = billed_counts(totals, expected)
    return all(billed[model_key] == expected[model_key] for model_key in expected)


def shortfall(totals, expected):
    """Per-model rows naming what the analytics never accounted for."""
    billed = billed_counts(totals, expected)
    return [
        {"model": model_key, "requests_sent": expected[model_key],
         "requests_billed": billed[model_key],
         "delta": billed[model_key] - expected[model_key]}
        for model_key in sorted(expected)
        if billed[model_key] != expected[model_key]
    ]


class Settle:
    """One window's analytics read, and whether it is a measurement at all.

    `complete` is false when the timeout was reached with the counts still
    disagreeing. `totals` is then the last read, kept only so the disagreement can
    be reported; nothing may be scored from it.

    `lag_seconds` is what was observed, not inferred: seconds from the window's own
    end to the first read that accounted for every request. `confirmed_seconds` is
    the same for the read that confirmed it.
    """

    def __init__(self, totals, complete, lag_seconds, confirmed_seconds, polls, missing):
        self.totals = totals
        self.complete = complete
        self.lag_seconds = lag_seconds
        self.confirmed_seconds = confirmed_seconds
        self.polls = polls
        self.missing = missing


def settled_window(session, start, end, models, expected, fake=None):
    """Poll a window until every model's billed count equals what it sent.

    Completeness is confirmed over ANALYTICS_SETTLE_COMPLETE_READS consecutive reads, so
    a count that matches once while data is still moving is not mistaken for the
    end. Reaching the timeout without completeness returns a Settle with
    `complete` false and the per-model shortfall in `missing`.
    """
    if fake is not None:
        print("  dry run, not polling for the analytics to settle")
        missing = shortfall(fake, expected)
        return Settle(fake, not missing, None, None, 0, missing)

    closed = end
    deadline = time.monotonic() + cfg.ANALYTICS_SETTLE_TIMEOUT_S
    completions = 0
    polls = 0
    first_complete_lag = None
    totals = {}
    while True:
        totals = read_window(session, start, end, models)
        polls += 1
        lag = (datetime.now(timezone.utc) - closed).total_seconds()
        if complete_read(totals, expected):
            completions += 1
            if first_complete_lag is None:
                first_complete_lag = lag
            if completions >= cfg.ANALYTICS_SETTLE_COMPLETE_READS:
                print(f"  complete after {first_complete_lag:.0f} s, confirmed at "
                      f"{lag:.0f} s, {polls} reads: every model billed exactly what "
                      f"the window sent")
                return Settle(totals, True, first_complete_lag, lag, polls, [])
        else:
            completions = 0
            first_complete_lag = None
        missing = shortfall(totals, expected)
        if time.monotonic() >= deadline:
            print(f"  gave up after {cfg.ANALYTICS_SETTLE_TIMEOUT_S} s with the counts still "
                  f"disagreeing: {window_log.describe_mismatches(missing)}")
            print("  this window is recorded as failed, not as a result")
            return Settle(totals, False, None, None, polls, missing)
        if missing:
            print(f"    {lag:.0f} s after the window closed, still short: "
                  f"{window_log.describe_mismatches(missing)}")
        else:
            print(f"    {lag:.0f} s after the window closed: every count matches, "
                  f"{completions} of {cfg.ANALYTICS_SETTLE_COMPLETE_READS} confirming reads")
        quiet.sleep_with_progress(cfg.ANALYTICS_POLL_INTERVAL_S,
                                  "still quiet, waiting for every request to be billed")


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
    """What the worker would report, from the same duration it would measure.

    Billed seconds are the measured quantity and are synthesised here. Neurons
    are the cross-check and are left out: the worker publishes cost per audio
    minute, not a neuron rate, so a dry run has none to invent.
    """
    return {
        "audio_seconds": variant["duration_s"],
        "neurons": None,
        "transcribe_ms": int(300 + variant["duration_s"] * 90),
    }


def fake_window(worker):
    """Billing as it would look if the file's own duration were billed."""
    return {
        key: {
            "requests": row["requests"],
            "audio_seconds": row["file_seconds"],
            "neurons": 0.0,
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
    if cfg.BASELINE_SPEED not in args.speeds:
        raise SystemExit(f"--speeds must include the {cfg.BASELINE_SPEED:g}x baseline to compare against")
    if args.max_windows is not None and args.max_windows < 1:
        raise SystemExit("--max-windows must be at least 1")
    if not cfg.VARIANTS.exists():
        raise SystemExit(f"missing {cfg.VARIANTS}; run compress.py first")

    cfg.catalogue()
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
        total_minutes * cfg.usd_per_audio_minute(m) for m in args.models
    ) * args.replicates
    print(f"P1 billing probe: {args.clips} clips at "
          f"{', '.join(f'{s:g}x' for s in args.speeds)}, {args.replicates} replicates")
    print(f"  models {', '.join(args.models)}")
    print(f"  {total_minutes:.1f} audio minutes per model per replicate, ~${cost:.2f} total")
    print(f"  the account only has to be free of other dictation during each window: the query "
          f"filters on the four speech models and requestSource {cfg.REQUEST_SOURCE!r}, "
          f"so unrelated AI traffic is already excluded")

    out_path = cfg.RUN_DIR / args.out if args.out else cfg.BILLING_PROBE_RESULT
    checkpoint = cfg.probe_windows_path(out_path, args.dry_run)
    shape = window_log.billing_shape(args.clips, args.models)
    log = window_log.load_windows(
        checkpoint, args.dry_run, shape, cfg.probe_windows_path(out_path, not args.dry_run))
    measured = log.measured
    # Same order as quiet.billing_windows, so a plan index is a window index.
    planned = [
        {"key": window_log.billing_key(replicate, speed),
         "label": f"replicate {replicate} of {args.replicates}, {speed:g}x",
         "replicate": replicate, "speed": speed}
        for replicate in range(1, args.replicates + 1)
        for speed in args.speeds
    ]

    schedule = quiet.QuietSchedule(
        quiet.billing_windows(args.speeds, args.replicates, args.clips, args.models, pool),
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
            f"probe_billing.py {'--dry-run' if args.dry_run else '--live'}"):
        print(f"  {line}")
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

    measured_now = 0
    for window_index, plan in enumerate(planned):
        if plan["key"] in measured:
            continue
        if args.max_windows is not None and measured_now >= args.max_windows:
            print(f"\nstopping after {measured_now} window"
                  f"{'s' if measured_now != 1 else ''}, as --max-windows asked")
            break
        speed = plan["speed"]
        print(f"\nreplicate {plan['replicate']} / {args.replicates}, {speed:g}x")
        schedule.open(window_index)
        variants = batches[speed]
        start = floor_minute(datetime.now(timezone.utc))
        worker = run_batch(session, variants, args.models, base_url, token, args.dry_run)
        end = floor_minute(datetime.now(timezone.utc)) + timedelta(minutes=1)
        print(f"  window {iso(start)} to {iso(end)}")
        wait_for_boundary(end, args.dry_run)

        settle = settled_window(
            session, start, end, args.models, sent_counts(worker),
            fake=fake_window(worker) if args.dry_run else None,
        )
        rows = compare(settle.totals, worker)
        # The window is complete only here, past the send and the settle, so this
        # is the first point at which anything about it may be written down.
        record = {
            "probe": "billing",
            "synthetic": args.dry_run,
            "window_key": plan["key"],
            "window_shape": shape,
            "replicate": plan["replicate"],
            "speed": speed,
            "clips": len(variants),
            "measured_at": window_log.now_iso(),
            "window_start": iso(start),
            "window_end": iso(end),
            "settled": settle.complete,
            "settle_seconds_observed": settle.lag_seconds,
            "settle_seconds_confirmed": settle.confirmed_seconds,
            "analytics_reads": settle.polls,
            "models": rows,
        }
        if not settle.complete:
            record["shortfall"] = settle.missing
        window_log.append_window(checkpoint, record)
        # A failed window is written down so the operator can see it, and is left out
        # of the measured set so it is re-measured rather than scored. It cost the
        # same money as a good one, so it still counts against --max-windows.
        measured_now += 1
        if settle.complete:
            measured[plan["key"]] = record
            schedule.observe(settle.lag_seconds)
            hold_seconds = quiet.boundary_hold_seconds(schedule.observed_settles)
            schedule.mark_done(window_index)
        else:
            log.corrupt[plan["key"]] = record
        schedule.close(window_index, settle_seconds=settle.lag_seconds)
        for row in rows:
            ratio = "n/a" if row["audio_seconds_ratio"] is None else f"{row['audio_seconds_ratio']:.4f}"
            print(f"  {row['model']:<17} billed {row['audio_seconds_billed']:>9.1f} s  "
                  f"sent {row['audio_seconds_sent']:>9.1f} s  ratio {ratio}")
        if settle.complete:
            print(f"  checkpointed window {window_index + 1} of {len(planned)} to "
                  f"{checkpoint.name}")
        else:
            print(f"  recorded window {window_index + 1} of {len(planned)} as failed in "
                  f"{checkpoint.name}: {window_log.describe_mismatches(settle.missing)}")
            print("  it is not a measurement and will be re-measured; no ratio is computed "
                  "from it")
        hold_boundary_gap(end, hold_seconds, args.dry_run)

    if any(plan["key"] not in measured for plan in planned):
        print("\n" + window_log.progress_line(planned, measured, "P1 billing probe"))
        for line in window_log.recovery_lines(
                planned, log.corrupt, checkpoint,
                f"probe_billing.py {'--dry-run' if args.dry_run else '--live'}"):
            print(line)
        return 0

    # Every window exists, so the replicates can be rebuilt in plan order. Pairing
    # is by key, not by the order the windows were measured in, so P1 compares the
    # 1x and 3x windows of the same replicate however the run was split up.
    replicates = [
        {"replicate": replicate,
         "windows": [window_log.result_fields(measured[window_log.billing_key(replicate, speed)])
                     for speed in args.speeds]}
        for replicate in range(1, args.replicates + 1)
    ]

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
    span = window_log.measurement_span(w.get("measured_at") for rep in replicates
                                       for w in rep["windows"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "probe": "P1 billed duration under compression",
        "synthetic": args.dry_run,
        "speeds": args.speeds,
        "clips_per_window": args.clips,
        "audio_minutes_per_replicate": total_minutes,
        "request_source_filter": cfg.REQUEST_SOURCE,
        "tolerance": TOLERANCE,
        "measurement_span": span,
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
    if span:
        print(f"windows measured between {span['first']} and {span['last']}, "
              f"spanning {span['days']:.2f} days")
        if span["days"] >= 1:
            print("that gap is a possible source of drift: the account's billing behaviour "
                  "may not have been the same across it")
    if args.dry_run:
        print("synthetic: the billed side was generated from the durations that were sent, so "
              "agreement here proves the arithmetic runs, not that billing is proportional")
    return 0


if __name__ == "__main__":
    main()
