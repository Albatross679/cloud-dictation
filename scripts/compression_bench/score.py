"""Stage 4: score the responses of one mode and write that mode's results file.

The mode is explicit and matches stage 3: --dry-run reads responses.dry-run.jsonl
and writes results.dry-run.json, --live reads responses.jsonl and writes
results.json. A log holding responses from the other mode stops the run, so a
results file is always scored from one kind of response.

Reference and hypothesis pass through the same Whisper English normalizer
before jiwer measures them, which is the protocol the Open ASR Leaderboard uses
and the reason absolute numbers here are comparable to published ones.

Confidence intervals are a paired bootstrap over utterances: the same utterance
appears at every speed, so resampling utterances rather than cells is what makes
the interval on the difference honest.
"""

import argparse
import json
import math
from collections import defaultdict

import numpy as np
from jiwer import process_words
from whisper_normalizer.english import EnglishTextNormalizer

import config as cfg
import response_log

normalize = EnglishTextNormalizer()


def is_repetition_loop(words):
    """True when the tail of the transcript is one fragment repeated."""
    n = cfg.REPETITION_NGRAM
    if len(words) < n * cfg.REPETITION_MIN_REPEATS:
        return False
    grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    counts = defaultdict(int)
    for gram in grams:
        counts[gram] += 1
    return max(counts.values()) >= cfg.REPETITION_MIN_REPEATS


def score_cell(reference, hypothesis):
    ref = normalize(reference)
    hyp = normalize(hypothesis or "")
    ref_words = ref.split()
    if not ref_words:
        return None
    if not hyp.split():
        return {"errors": len(ref_words), "ref_words": len(ref_words),
                "sub": 0, "dele": len(ref_words), "ins": 0,
                "wer": 1.0, "catastrophic": True, "loop": False}
    out = process_words(ref, hyp)
    errors = out.substitutions + out.deletions + out.insertions
    wer = errors / len(ref_words)
    loop = is_repetition_loop(hyp.split())
    return {
        "errors": errors,
        "ref_words": len(ref_words),
        "sub": out.substitutions,
        "dele": out.deletions,
        "ins": out.insertions,
        "wer": wer,
        "loop": loop,
        "catastrophic": wer > cfg.CATASTROPHIC_WER or loop,
    }


def corpus_wer(cells):
    """Corpus WER: total errors over total reference words, not a mean of ratios."""
    errors = sum(c["errors"] for c in cells)
    words = sum(c["ref_words"] for c in cells)
    return errors / words if words else float("nan")


def paired_bootstrap(by_utt, speed, baseline, rng):
    """Interval on the corpus-level WER difference between a speed and 1.0x."""
    utts = sorted(by_utt)
    if not utts:
        return (float("nan"), float("nan"))
    fast_e = np.array([by_utt[u][speed]["errors"] for u in utts], dtype=float)
    fast_w = np.array([by_utt[u][speed]["ref_words"] for u in utts], dtype=float)
    base_e = np.array([by_utt[u][baseline]["errors"] for u in utts], dtype=float)
    base_w = np.array([by_utt[u][baseline]["ref_words"] for u in utts], dtype=float)
    n = len(utts)
    deltas = np.empty(cfg.BOOTSTRAP_RESAMPLES)
    for i in range(cfg.BOOTSTRAP_RESAMPLES):
        pick = rng.integers(0, n, n)
        deltas[i] = fast_e[pick].sum() / fast_w[pick].sum() - base_e[pick].sum() / base_w[pick].sum()
    return tuple(np.percentile(deltas, [2.5, 97.5]) * 100)


def percentile(values, q):
    return float(np.percentile(values, q)) if values else None


def histogram_bins(values, step=10):
    """Bins wide enough to hold every value, so nothing falls outside the chart."""
    lo = int(math.floor(min(values) / step) * step)
    hi = int(math.ceil(max(values) / step) * step)
    return list(range(lo, hi + step, step))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="score the synthesised responses of a dry run")
    mode.add_argument("--live", action="store_true",
                      help="score the measured responses of a live run")
    parser.add_argument("--responses", default=None,
                        help="override the response log path; it is still checked against the mode")
    parser.add_argument("--out", default=None, help="override the results path")
    args = parser.parse_args()

    responses_path = (cfg.RUN_DIR / args.responses if args.responses
                      else cfg.responses_path(args.dry_run))
    results_path = cfg.RUN_DIR / args.out if args.out else cfg.results_path(args.dry_run)
    if not responses_path.exists():
        stage = "run.py --dry-run" if args.dry_run else "run.py --live"
        raise SystemExit(f"missing {responses_path}; run {stage} first")
    responses = response_log.verify_responses(responses_path, args.dry_run)

    manifest = {}
    with open(cfg.MANIFEST) as handle:
        for line in handle:
            record = json.loads(line)
            manifest[record["utt_id"]] = record

    scored = defaultdict(dict)   # model -> utt -> speed -> cell
    latency = defaultdict(list)  # (model, speed) -> ms
    texts = {}                   # (model, utt, speed) -> hypothesis
    failures = 0
    synthetic = 0
    total = 0
    for response in responses:
        if not response.get("ok"):
            failures += 1
            continue
        total += 1
        synthetic += 1 if response.get("synthetic") else 0
        reference = manifest[response["utt_id"]]["reference"]
        cell = score_cell(reference, response.get("text", ""))
        if cell is None:
            continue
        cell["wpm_effective"] = response["wpm_effective"]
        scored[response["model"]].setdefault(response["utt_id"], {})[response["speed"]] = cell
        texts[(response["model"], response["utt_id"], response["speed"])] = response.get("text", "")
        if response.get("transcribe_ms") is not None:
            latency[(response["model"], response["speed"])].append(response["transcribe_ms"])

    rng = np.random.default_rng(cfg.BOOTSTRAP_SEED)
    grid = []
    recommended = {}
    gallery = []

    for model_key in cfg.MODELS:
        per_utt = scored.get(model_key, {})
        if not per_utt:
            continue
        speeds = sorted({speed for cells in per_utt.values() for speed in cells})
        if cfg.BASELINE_SPEED not in speeds:
            print(f"{model_key}: no {cfg.BASELINE_SPEED:g}x baseline, skipped")
            continue
        by_utt = {u: s for u, s in per_utt.items() if len(s) == len(speeds)}
        if not by_utt:
            print(f"{model_key}: no utterance covers every speed, skipped")
            continue
        if len(by_utt) < len(per_utt):
            print(f"{model_key}: {len(per_utt) - len(by_utt)} utterances dropped, not scored at every speed")

        baseline_cells = [by_utt[u][cfg.BASELINE_SPEED] for u in by_utt]
        baseline_wer = corpus_wer(baseline_cells)
        baseline_cat = sum(c["catastrophic"] for c in baseline_cells) / len(baseline_cells)

        best = None
        run_intact = True
        for speed in speeds:
            cells = [by_utt[u][speed] for u in by_utt]
            wer = corpus_wer(cells)
            delta = (wer - baseline_wer) * 100
            lo, hi = ((0.0, 0.0) if speed == cfg.BASELINE_SPEED
                      else paired_bootstrap(by_utt, speed, cfg.BASELINE_SPEED, rng))
            cat = sum(c["catastrophic"] for c in cells) / len(cells)
            words = sum(c["ref_words"] for c in cells)
            lat = latency.get((model_key, speed), [])
            passes = (
                speed == cfg.BASELINE_SPEED
                or (delta <= cfg.DELTA_WER_BUDGET
                    and hi <= cfg.DELTA_WER_CI_CEILING
                    and (cat - baseline_cat) * 100 <= cfg.CATASTROPHIC_BUDGET)
            )
            # The recommendation is the fastest speed reachable without passing
            # through one that fails, so a lucky cell above a failure cannot be
            # recommended.
            if speed > cfg.BASELINE_SPEED:
                if passes and run_intact:
                    best = speed
                elif not passes:
                    run_intact = False
            grid.append({
                "model": model_key,
                "speed": speed,
                "utterances": len(by_utt),
                "wer": wer * 100,
                "delta_wer": delta,
                "delta_ci": [lo, hi],
                "sub_rate": sum(c["sub"] for c in cells) / words * 100,
                "del_rate": sum(c["dele"] for c in cells) / words * 100,
                "ins_rate": sum(c["ins"] for c in cells) / words * 100,
                "catastrophic": cat * 100,
                "loops": sum(c["loop"] for c in cells),
                "latency_p50": percentile(lat, 50),
                "latency_p95": percentile(lat, 95),
                "usd_per_hour": cfg.usd_per_hour(model_key, speed),
                "free_minutes_per_day": cfg.free_minutes_per_day(model_key, speed),
                "saving_pct": (1 - 1 / speed) * 100,
                "passes": passes,
            })
        recommended[model_key] = best

        # Worst cells at the fastest speed run, so the report can show what
        # breaking actually looks like rather than only how often it happens.
        top_speed = speeds[-1]
        worst = sorted(
            ((u, by_utt[u][top_speed]) for u in by_utt),
            key=lambda item: item[1]["wer"], reverse=True,
        )[: cfg.GALLERY_EXAMPLES]
        for utt_id, cell in worst:
            gallery.append({
                "model": model_key,
                "speed": top_speed,
                "utt_id": utt_id,
                "wer": cell["wer"] * 100,
                "loop": cell["loop"],
                "reference": manifest[utt_id]["reference"],
                "hypothesis": texts.get((model_key, utt_id, top_speed), ""),
            })

    # Error against effective speaking rate, pooled over models and speeds.
    all_cells = [c for model in scored.values() for utt in model.values() for c in utt.values()]
    rate_curve = []
    if all_cells:
        rates = [c["wpm_effective"] for c in all_cells]
        edges = histogram_bins(rates, step=40)
        for model_key in cfg.MODELS:
            for lo, hi in zip(edges, edges[1:]):
                cells = [c for utt in scored.get(model_key, {}).values() for c in utt.values()
                         if lo <= c["wpm_effective"] < hi]
                if len(cells) < 20:
                    continue
                rate_curve.append({
                    "model": model_key,
                    "wpm_lo": lo,
                    "wpm_hi": hi,
                    "n": len(cells),
                    "wer": corpus_wer(cells) * 100,
                })

    durations = [r["duration_s"] for r in manifest.values()]
    wpms = [r["wpm"] for r in manifest.values()]
    rates = sorted(wpms)
    wpm_bins = histogram_bins(wpms, step=10)
    results = {
        "config": {
            "corpus": "LibriSpeech test-clean",
            "sample_size": cfg.SAMPLE_SIZE,
            "seed": cfg.SAMPLE_SEED,
            "speeds": sorted({row["speed"] for row in grid}),
            "models": {k: v["label"] for k, v in cfg.MODELS.items()},
            "delta_wer_budget": cfg.DELTA_WER_BUDGET,
            "catastrophic_budget": cfg.CATASTROPHIC_BUDGET,
        },
        "corpus": {
            "utterances": len(manifest),
            "total_minutes": sum(durations) / 60,
            "mean_duration_s": sum(durations) / len(durations),
            "words": sum(r["words"] for r in manifest.values()),
            "wpm_median": rates[len(rates) // 2],
            "wpm_min": rates[0],
            "wpm_max": rates[-1],
            "wpm_histogram": np.histogram(wpms, bins=wpm_bins)[0].tolist(),
            "wpm_bins": wpm_bins,
        },
        "grid": grid,
        "rate_curve": rate_curve,
        "gallery": gallery,
        "recommended": recommended,
        "failures": failures,
        "responses": total,
        "synthetic": synthetic,
        "mode": "dry-run" if args.dry_run else "live",
    }
    results_path.write_text(json.dumps(results, indent=2))

    print(f"wrote {results_path}")
    if synthetic:
        print(f"{synthetic} of {total} responses are synthetic; this is a dry run, not a measurement")
    if failures:
        print(f"{failures} failed responses excluded")
    print(f"\n{'model':<17}{'r':>6}{'WER':>8}{'dWER':>8}{'95% CI':>16}{'catas':>8}   verdict")
    for row in grid:
        ci = f"[{row['delta_ci'][0]:.1f}, {row['delta_ci'][1]:.1f}]"
        verdict = "baseline" if row["speed"] == cfg.BASELINE_SPEED else ("within" if row["passes"] else "over")
        print(f"{row['model']:<17}{row['speed']:>5g}x{row['wer']:>7.1f}%{row['delta_wer']:>+8.1f}"
              f"{ci:>16}{row['catastrophic']:>7.1f}%   {verdict}")
    print("\nrecommended operating speed")
    for model_key, speed in recommended.items():
        print(f"  {model_key:<17}{'compression does not pay' if not speed else f'{speed:g}x'}")


if __name__ == "__main__":
    main()
