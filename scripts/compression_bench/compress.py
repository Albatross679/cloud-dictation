"""Stage 2: build the time-compressed variant of every sampled utterance.

Compression is pitch preserving (ffmpeg atempo) and the codec is held constant
at 16 kHz mono PCM, so duration is the only thing that changes between speeds.
Writes runs/compression-bench/audio/variants.jsonl.
"""

import argparse
import json
import shutil
import subprocess
import sys

import config as cfg


def atempo_chain(speed):
    """atempo filter string, chained for builds that cap one instance at 2.0."""
    factors = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={f:g}" for f in factors)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def compress(src, dst, speed):
    if speed == 1.0:
        shutil.copyfile(src, dst)
        return
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-filter:a", atempo_chain(speed),
         "-ar", str(cfg.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtol", type=float, default=0.01,
                        help="allowed relative error between asked and actual duration")
    parser.add_argument("--atol", type=float, default=0.06,
                        help="allowed absolute error in seconds, on top of the relative one")
    args = parser.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")
    if not cfg.MANIFEST.exists():
        sys.exit(f"missing {cfg.MANIFEST}; run prepare_corpus.py first")

    manifest = [json.loads(line) for line in open(cfg.MANIFEST)]
    cfg.AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    variants = []
    worst_rel = (0.0, None)
    worst_abs = (0.0, None)
    for speed in cfg.SPEEDS:
        out_dir = cfg.AUDIO_DIR / f"speed-{speed:g}"
        out_dir.mkdir(exist_ok=True)
        worst_here = (0.0, 0.0, None)
        for i, record in enumerate(manifest, 1):
            src = cfg.RUN_DIR / record["path"]
            dst = out_dir / f"{record['utt_id']}.wav"
            if not dst.exists():
                compress(src, dst, speed)
            actual = probe_duration(dst)
            expected = record["duration_s"] / speed
            off = actual - expected
            error = abs(off) / expected
            where = f"{record['utt_id']} {actual:.3f}s vs {expected:.3f}s"
            if error > worst_rel[0]:
                worst_rel = (error, f"{speed:g}x {where}")
            if abs(off) > worst_abs[0]:
                worst_abs = (abs(off), f"{speed:g}x {where}")
            if error > worst_here[0]:
                worst_here = (error, abs(off), where)
            # atempo lands within a frame or two of the asked duration, an
            # offset that is fixed in seconds and so largest in relative terms on
            # the shortest clips. The check allows both.
            if abs(off) > args.atol + args.rtol * expected:
                sys.exit(f"{dst.name} at {speed}x is {actual:.3f}s, expected {expected:.3f}s "
                         f"({off:+.3f}s, over the {args.atol:g}s + {args.rtol:.0%} allowance)")
            variants.append({
                "utt_id": record["utt_id"],
                "speed": speed,
                "path": str(dst.relative_to(cfg.RUN_DIR)),
                "duration_s": round(actual, 3),
                "source_duration_s": record["duration_s"],
                "wpm_effective": round(record["wpm"] * speed, 1),
            })
            if i % 100 == 0:
                print(f"  {speed:g}x: {i} / {len(manifest)}")
        print(f"{speed:g}x done, worst duration error {worst_here[0] * 100:.3f}% "
              f"({worst_here[1] * 1000:.0f} ms, {worst_here[2]})")

    with open(cfg.VARIANTS, "w") as out:
        for variant in variants:
            out.write(json.dumps(variant) + "\n")

    billed = sum(v["duration_s"] for v in variants) / 60
    source = sum(r["duration_s"] for r in manifest) / 60
    asked = sum(v["source_duration_s"] / v["speed"] for v in variants) / 60
    print(f"\nwrote {cfg.VARIANTS}")
    print(f"  variants        {len(variants)}  ({len(manifest)} utterances x {len(cfg.SPEEDS)} speeds)")
    print(f"  source audio    {source:.1f} min")
    print(f"  billed audio    {billed:.1f} min per model")
    print(f"  worst error     {worst_rel[0] * 100:.2f}% relative  ({worst_rel[1]})")
    print(f"                  {worst_abs[0] * 1000:.0f} ms absolute  ({worst_abs[1]})")
    print(f"  pooled error    {(billed - asked) / asked * 100:+.3f}% over all {len(variants)} variants")
    for key, model in cfg.MODELS.items():
        print(f"  {key:<16} ${billed * model['neurons_per_audio_minute'] * cfg.USD_PER_1000_NEURONS / 1000:.4f}")


if __name__ == "__main__":
    main()
