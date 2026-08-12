"""Stage 1: fetch LibriSpeech test-clean and sample the utterances to score.

Writes runs/compression-bench/corpus/manifest.jsonl, one record per utterance
with its reference transcript, duration, word count and baseline speaking rate.
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
import tarfile
import urllib.request

import config as cfg


def download(dest):
    if dest.exists():
        print(f"archive present: {dest} ({dest.stat().st_size / 1e6:.0f} MB)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {cfg.CORPUS_URL} (~{cfg.CORPUS_ARCHIVE_MB} MB)")
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(cfg.CORPUS_URL) as response, open(tmp, "wb") as out:
        total = int(response.headers.get("content-length", 0))
        seen = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            seen += len(chunk)
            if total:
                print(f"\r  {seen / 1e6:6.0f} / {total / 1e6:.0f} MB", end="", flush=True)
        print()
    tmp.rename(dest)


def extract(archive):
    print(f"extracting into {cfg.CORPUS_DIR}")
    with tarfile.open(archive) as tar:
        tar.extractall(cfg.CORPUS_DIR, filter="data")


def read_transcripts():
    """Every (utterance id, flac path, reference) triple in test-clean."""
    items = []
    for trans in sorted(cfg.CORPUS_ROOT.rglob("*.trans.txt")):
        for line in trans.read_text().splitlines():
            if not line.strip():
                continue
            utt_id, _, text = line.partition(" ")
            flac = trans.parent / f"{utt_id}.flac"
            if flac.exists():
                items.append((utt_id, flac, text.strip()))
    return items


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def to_wav(src, dst):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-ar", str(cfg.SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-archive", action="store_true",
                        help="keep the downloaded tar.gz after extraction")
    args = parser.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")

    archive = cfg.CORPUS_DIR / "test-clean.tar.gz"
    if cfg.CORPUS_ROOT.exists():
        print(f"corpus present: {cfg.CORPUS_ROOT}")
    else:
        download(archive)
        extract(archive)

    items = read_transcripts()
    print(f"test-clean holds {len(items)} utterances")

    rng = random.Random(cfg.SAMPLE_SEED)
    rng.shuffle(items)

    wav_dir = cfg.CORPUS_DIR / "wav"
    wav_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for utt_id, flac, text in items:
        if len(records) == cfg.SAMPLE_SIZE:
            break
        duration = probe_duration(flac)
        if duration > cfg.MAX_UTTERANCE_SECONDS:
            continue
        wav = wav_dir / f"{utt_id}.wav"
        if not wav.exists():
            to_wav(flac, wav)
        words = len(text.split())
        records.append({
            "utt_id": utt_id,
            "path": str(wav.relative_to(cfg.RUN_DIR)),
            "reference": text,
            "duration_s": round(duration, 3),
            "words": words,
            "wpm": round(words / duration * 60, 1),
        })
        if len(records) % 50 == 0:
            print(f"  prepared {len(records)} / {cfg.SAMPLE_SIZE}")

    if len(records) < cfg.SAMPLE_SIZE:
        sys.exit(f"only {len(records)} utterances met the {cfg.MAX_UTTERANCE_SECONDS}s cap")

    cfg.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.MANIFEST, "w") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")

    total_s = sum(r["duration_s"] for r in records)
    total_words = sum(r["words"] for r in records)
    rates = sorted(r["wpm"] for r in records)
    durations = sorted(r["duration_s"] for r in records)
    print(f"\nwrote {cfg.MANIFEST}")
    print(f"  utterances     {len(records)}")
    print(f"  total audio    {total_s / 60:.1f} min ({total_s:.1f} s)")
    print(f"  mean duration  {total_s / len(records):.2f} s  "
          f"(median {durations[len(durations) // 2]:.2f}, "
          f"range {durations[0]:.2f} to {durations[-1]:.2f})")
    print(f"  words          {total_words}")
    print(f"  rate median    {rates[len(rates) // 2]:.1f} wpm  "
          f"(range {rates[0]:.1f} to {rates[-1]:.1f})")

    if not args.keep_archive and archive.exists():
        archive.unlink()
        print(f"  removed {archive.name}")


if __name__ == "__main__":
    main()
