"""The response log, and the guard that keeps the two modes apart.

Every response carries `synthetic`: true when a dry run invented it, false when a
live run measured it. Each mode owns its own log, results file and report, so the
two kinds never share a file. The guard here checks the records themselves rather
than the file name, so a file that was copied, renamed or written by an older
version of the harness is still caught, and any stage that opens a log whose
records disagree with its mode stops instead of producing a blended number.
"""

import json

import config as cfg


def read_records(path):
    """Every decodable record in a log, in file order."""
    records = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def count_modes(records):
    """(synthetic, real) counts over a list of records."""
    synthetic = sum(1 for record in records if record.get("synthetic"))
    return synthetic, len(records) - synthetic


def verify_responses(path, dry_run, records=None):
    """Return the log's records, or raise SystemExit if any belongs to the other mode."""
    if records is None:
        if not path.exists():
            return []
        records = read_records(path)
    synthetic, real = count_modes(records)
    mine, theirs = (synthetic, real) if dry_run else (real, synthetic)
    if not theirs:
        return records

    this_mode = "dry run" if dry_run else "live run"
    that_mode = "live run" if dry_run else "dry run"
    if mine:
        raise SystemExit(
            f"{path} is mixed: {synthetic} synthetic records, {real} real. "
            f"Nothing downstream can tell them apart, so this file cannot be scored or resumed. "
            f"Move it out of the way and start this {this_mode} from an empty log."
        )
    raise SystemExit(
        f"{path} holds {theirs} records from a {that_mode}, and this is a {this_mode}. "
        f"Each mode owns its own log: {cfg.responses_path(dry_run).name} for a {this_mode}, "
        f"{cfg.responses_path(not dry_run).name} for a {that_mode}. If this log predates the "
        f"split, rename it to {cfg.responses_path(not dry_run)} and re-run this command."
    )


def verify_results(path, dry_run, results):
    """Raise SystemExit unless a results file was scored from this mode's responses."""
    synthetic = results.get("synthetic", 0)
    real = results.get("responses", 0) - synthetic
    if dry_run and real:
        raise SystemExit(
            f"{path} was scored from {real} real responses, and this is a dry run. "
            f"Score the dry-run log first: score.py --dry-run."
        )
    if not dry_run and synthetic:
        raise SystemExit(
            f"{path} was scored from {synthetic} synthetic responses, and this is a live run. "
            f"Score the live log first: score.py --live."
        )
