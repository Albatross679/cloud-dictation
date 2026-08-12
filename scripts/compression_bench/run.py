"""Stage 3: send every (utterance, speed, model) cell to the worker.

Appends one JSON record per response to the resume log its mode owns:
runs/compression-bench/responses.jsonl for --live and responses.dry-run.jsonl for
--dry-run. A cell already present and successful in that log is skipped, so an
interrupted run continues where it stopped and never pays for the same cell
twice. Because the two modes write different files, a live run can never mistake
a synthesised response for work it has already paid for, and the log is checked
against the mode before anything is skipped.

The mode is explicit. --dry-run synthesises responses and touches no network,
which is how the pipeline is validated before any inference is bought. --live is
the only path that reaches the worker, and it prints the bill it is about to
incur before the first request.
"""

import argparse
import json
import math
import random
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import requests

import config as cfg
import response_log

_lock = threading.Lock()

# Shape of the synthesised error curve: flat, then a knee. The constants are
# chosen so a dry run lands some cells inside the acceptance rule and some
# outside it, which is what makes the report exercise both branches. They carry
# no claim about how these models actually behave.
DRY_RUN_BASE_ERROR = 0.03
DRY_RUN_KNEE = 0.008
DRY_RUN_EXPONENT = 6.0
DRY_RUN_MODEL_PENALTY = {"nova-3": 1.0, "whisper-turbo": 0.85, "whisper": 2.1, "whisper-tiny-en": 3.4}


def cell_key(record):
    return f"{record['utt_id']}|{record['speed']:g}|{record['model']}"


def load_done(path, dry_run):
    """Cells this mode has already completed, after checking the log is this mode's."""
    records = response_log.verify_responses(path, dry_run)
    return {cell_key(r) for r in records if r.get("ok") and bool(r.get("synthetic")) == dry_run}


def transcribe(session, variant, model_key, base_url, token):
    params = {"model": model_key, "cleanup": str(cfg.CLEANUP)}
    if cfg.MODELS[model_key]["honors_language"]:
        params["language"] = cfg.LANGUAGE
    if cfg.VOCABULARY:
        params["vocabulary"] = cfg.VOCABULARY

    audio = (cfg.RUN_DIR / variant["path"]).read_bytes()
    started = time.monotonic()
    response = session.post(
        f"{base_url}/transcribe",
        params=params,
        data=audio,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "audio/wav"},
        timeout=cfg.REQUEST_TIMEOUT_S,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    response.raise_for_status()
    body = response.json()
    return {
        "text": body.get("text", ""),
        "audio_seconds": body.get("audio_seconds"),
        "neurons": body.get("neurons"),
        "transcribe_ms": body.get("transcribe_ms"),
        "round_trip_ms": elapsed_ms,
        "language_mismatch": body.get("language_mismatch"),
    }


def retryable(err):
    """A 4xx other than 429 is the worker rejecting the request, not a wobble."""
    response = getattr(err, "response", None)
    if response is None:
        return True
    return response.status_code == 429 or response.status_code >= 500


def fake_transcribe(variant, model_key, reference, rng):
    """A plausible response, so scoring and reporting run without the network."""
    degradation = DRY_RUN_BASE_ERROR + DRY_RUN_KNEE * (variant["speed"] - 1.0) ** DRY_RUN_EXPONENT
    penalty = DRY_RUN_MODEL_PENALTY.get(model_key, 1.0)
    words = reference.split()
    kept = []
    for word in words:
        roll = rng.random()
        if roll < degradation * penalty * 0.35:
            continue
        if roll < degradation * penalty * 0.7:
            kept.append(word[::-1])
            continue
        kept.append(word)
    if rng.random() < max(0.0, (degradation * penalty - 0.35)) * 0.5 and words:
        kept = (words[:3] + words[1:3] * 6)[:len(words)]
    rate = cfg.MODELS[model_key]["neurons_per_audio_minute"]
    return {
        "text": " ".join(kept),
        "audio_seconds": variant["duration_s"],
        "neurons": round(variant["duration_s"] / 60 * rate, 4),
        "transcribe_ms": int(400 + variant["duration_s"] * 60 * rng.uniform(0.9, 1.3)),
        "round_trip_ms": None,
        "language_mismatch": None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="synthesise responses instead of calling the worker")
    mode.add_argument("--live", action="store_true",
                      help="send real requests to the worker and pay for them")
    parser.add_argument("--models", nargs="*", default=list(cfg.MODELS),
                        help="subset of models to run")
    parser.add_argument("--speeds", nargs="*", type=float, default=cfg.SPEEDS,
                        help="subset of speeds to run")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of utterances, for a smoke run")
    parser.add_argument("--out", default=None,
                        help="override the response log path; it is still checked against the mode")
    args = parser.parse_args()

    unknown = [m for m in args.models if m not in cfg.MODELS]
    if unknown:
        raise SystemExit(f"unknown models: {', '.join(unknown)}")
    if not cfg.VARIANTS.exists():
        raise SystemExit(f"missing {cfg.VARIANTS}; run compress.py first")

    out_path = cfg.RUN_DIR / args.out if args.out else cfg.responses_path(args.dry_run)
    manifest = {}
    with open(cfg.MANIFEST) as handle:
        for line in handle:
            record = json.loads(line)
            manifest[record["utt_id"]] = record
    with open(cfg.VARIANTS) as handle:
        variants = [json.loads(line) for line in handle]
    if args.limit:
        keep = set(sorted(manifest)[: args.limit])
        variants = [v for v in variants if v["utt_id"] in keep]

    cells = [
        (variant, model_key)
        for variant in variants
        if any(abs(variant["speed"] - s) < 1e-9 for s in args.speeds)
        for model_key in args.models
    ]
    done = load_done(out_path, args.dry_run)
    pending = [
        c for c in cells
        if cell_key({"utt_id": c[0]["utt_id"], "speed": c[0]["speed"], "model": c[1]}) not in done
    ]

    print(f"cells {len(cells)}, already done {len(done)}, pending {len(pending)}")
    if not pending:
        print("nothing to do")
        return

    base_url = token = session = None
    if args.live:
        billed = sum(v["duration_s"] for v, _ in pending) / 60
        cost = sum(
            v["duration_s"] / 60 * cfg.MODELS[m]["neurons_per_audio_minute"]
            for v, m in pending
        ) * cfg.USD_PER_1000_NEURONS / 1000
        print(f"about to send {len(pending)} requests, {billed:.1f} billed minutes, ~${cost:.2f}")
        base_url = cfg.worker_url()
        token = cfg.auth_token()
        session = requests.Session()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0, "fail": 0}
    started = time.monotonic()

    def work(handle, cell):
        variant, model_key = cell
        reference = manifest[variant["utt_id"]]["reference"]
        record = {
            "utt_id": variant["utt_id"],
            "speed": variant["speed"],
            "model": model_key,
            "duration_s": variant["duration_s"],
            "source_duration_s": variant["source_duration_s"],
            "wpm_effective": variant["wpm_effective"],
            "synthetic": args.dry_run,
        }
        try:
            if args.dry_run:
                rng = random.Random(zlib.crc32(cell_key(record).encode()))
                record.update(fake_transcribe(variant, model_key, reference, rng))
            else:
                for attempt in range(1, cfg.MAX_ATTEMPTS + 1):
                    try:
                        record.update(transcribe(session, variant, model_key, base_url, token))
                        break
                    except Exception as err:
                        if attempt == cfg.MAX_ATTEMPTS or not retryable(err):
                            raise
                        time.sleep(cfg.RETRY_BACKOFF_S * attempt)
            record["ok"] = True
        except Exception as err:
            record["ok"] = False
            record["error"] = f"{type(err).__name__}: {err}"

        with _lock:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            counter["n"] += 1
            counter["fail"] += 0 if record["ok"] else 1
            if counter["n"] % 100 == 0 or counter["n"] == len(pending):
                rate = counter["n"] / max(1e-9, time.monotonic() - started)
                left = (len(pending) - counter["n"]) / max(1e-9, rate)
                print(f"  {counter['n']} / {len(pending)}  {rate:.1f}/s  ~{left / 60:.1f} min left"
                      f"  failures {counter['fail']}")

    workers = 1 if args.dry_run else cfg.CONCURRENCY
    with open(out_path, "a") as handle:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda cell: work(handle, cell), pending))

    print(f"\nwrote {out_path}")
    if args.dry_run:
        print("responses are synthetic; every downstream number is shape, not measurement")
    if counter["fail"]:
        print(f"{counter['fail']} cells failed; re-run this command to retry only those")


if __name__ == "__main__":
    main()
