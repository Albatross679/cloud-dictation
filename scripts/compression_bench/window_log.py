"""Per-window checkpoints for the two probes.

A measurement window is self-contained: one batch sent, one settle waited out,
one analytics delta read. Nothing about window five needs window four still in
memory, so each probe appends the whole of a window's result to this log the
moment that window closes and settles. An interrupted probe keeps every window it
finished, and the captain can run the probes in short idle stretches instead of
one long sitting.

A window in flight is never checkpointed. Its measurement depends on an
uninterrupted send followed by a clean settle, so half of one is not salvageable:
the record is written only after the settle returns, and a line torn by a kill
mid-write fails to decode and is dropped on the next read. Either way that window
is discarded and re-measured whole.

The two modes are kept apart the way response_log.py keeps the grid's modes
apart: every record carries `synthetic`, each mode owns its own file, and the
records themselves are checked against the mode before anything is skipped.

Each record also carries the shape of the batch it measured and the time it was
measured at. A checkpoint whose shape disagrees with the run reading it stops the
run, because windows measured from different batches are not comparable. The
timestamps are reported and stored, so a run split across days shows the gap.

A checkpoint only counts as a measurement when every model's billed request count
has reached at least what that window sent, and the excess above that is within
what the platform itself produces. Analytics still arriving leave a count below
what was sent, and another source's traffic inside the window leaves an excess far
above the platform's; every ratio computed from either is meaningless. Those
records are held back from the measured set by load_windows, so the probe treats
the window as still to measure and no result is written from it. The checks read
the counts in the record itself, so a window written before they existed is
classified the same way.
"""

import json
from datetime import datetime, timezone

import config as cfg
import excess
import response_log


def now_iso():
    """Measurement time as the result files record it: UTC, second resolution."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    """A timestamp this module wrote, back as an aware datetime."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def measurement_span(timestamps):
    """First and last measurement time, and the days between them."""
    stamps = sorted(t for t in timestamps if t)
    if not stamps:
        return None
    first, last = parse_iso(stamps[0]), parse_iso(stamps[-1])
    return {
        "first": stamps[0],
        "last": stamps[-1],
        "days": (last - first).total_seconds() / 86400,
    }


def billing_key(replicate, speed):
    """Key of one P1 window. Replicate and speed together, so the 1x against 3x
    comparison inside a replicate cannot be satisfied by a window from another."""
    return f"replicate{replicate}|{speed:g}x"


def silence_key(padding):
    """Key of one P2 window."""
    return f"padding{padding:g}s"


def billing_shape(clips, models):
    """What a P1 window sends. Two windows are comparable only when these match."""
    return {"clips": int(clips), "models": sorted(models)}


def silence_shape(repeats, models, speech_seconds):
    """What a P2 window sends."""
    return {"repeats": int(repeats), "models": sorted(models),
            "speech_seconds": round(float(speech_seconds), 3)}


def undercounts(record):
    """Model rows of a record billed for less than the window sent them.

    An empty list means the whole bill for that window had arrived, which is the
    condition under which the window's audio-seconds ratio means anything. Billed
    above sent is the platform's own excess and is a result, not a fault.
    """
    return excess.undercounts(excess.record_rows(record))


def implausible_excess(record):
    """Model rows whose excess is above what the platform has been measured to produce.

    Such a window reads as another source's traffic having entered it, so it is
    refused and re-measured rather than averaged in.
    """
    return excess.implausible(excess.record_rows(record))


def refusal(record):
    """Why this window is not a measurement, or None when it is one.

    Read from the counts in the record itself, so a window written before these
    checks existed is classified the same way as one written after.
    """
    if record.get("settled") is False:
        return "recorded as failed"
    missing = undercounts(record)
    if missing:
        return f"billed for less than it sent: {excess.describe(missing)}"
    flagged = implausible_excess(record)
    if flagged:
        rows = excess.record_rows(record)
        return (f"excess above the {cfg.EXCESS_IMPLAUSIBLE_ABOVE:.0%} the platform produces, "
                f"which reads as foreign traffic in the window: {excess.describe(flagged)} "
                f"({excess.describe_rate(rows)})")
    return None


class WindowLog:
    """A probe's checkpoints, split into the windows that count and the ones that do not.

    `measured` holds the windows whose whole bill arrived and whose excess is the
    platform's own, keyed by window key. `corrupt` holds the rest: windows the
    analytics never fully accounted for, and windows carrying more excess than the
    platform produces. A key appearing more than once keeps its last record, so
    re-measuring a corrupt window supersedes it.
    """

    def __init__(self, records=()):
        latest = {}
        for record in records:
            latest[record["window_key"]] = record
        self.measured = {}
        self.corrupt = {}
        for key, record in latest.items():
            if refusal(record):
                self.corrupt[key] = record
            else:
                self.measured[key] = record

    def mismatches(self, key):
        return undercounts(self.corrupt[key])


def load_windows(path, dry_run, shape, counterpart):
    """This mode's checkpoints, classified into measured and corrupt windows.

    Raises SystemExit when a record belongs to the other mode, or when it was
    measured from a differently shaped batch.
    """
    if not path.exists():
        return WindowLog()
    records = response_log.read_records(path)
    response_log.verify_mode(path, dry_run, records, path, counterpart)
    for record in records:
        recorded = record.get("window_shape")
        if recorded != shape:
            raise SystemExit(
                f"{path} holds a window measured from a different batch: "
                f"{json.dumps(recorded, sort_keys=True)}, and this run sends "
                f"{json.dumps(shape, sort_keys=True)}. Windows from different batches "
                f"cannot be compared with each other. Move this file out of the way to "
                f"re-measure every window, or re-run with the arguments it was written with."
            )
    return WindowLog(records)


def append_window(path, record):
    """Write one completed window, before the next one opens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()


def result_fields(record):
    """A checkpoint record without the bookkeeping the result file does not carry."""
    return {key: value for key, value in record.items()
            if key not in ("probe", "synthetic", "window_key", "window_shape")}


def recovery_lines(planned, corrupt, path, command):
    """What to do about windows the analytics never fully accounted for.

    Named one by one with the reason they were refused, because such a window is
    not a measurement and the operator has to buy it again. Re-running the same
    command measures exactly these windows: they are not in the measured set, and
    the record a re-measurement appends supersedes the corrupt one under the same
    key.
    """
    if not corrupt:
        return []
    labels = {plan["key"]: plan["label"] for plan in planned}
    lines = [
        f"{len(corrupt)} window{'s' if len(corrupt) != 1 else ''} in {path.name} "
        f"{'were' if len(corrupt) != 1 else 'was'} read while the bill was still "
        f"arriving, or carried more traffic than the platform's own excess accounts "
        f"for. They are not measurements and nothing is scored from them:"
    ]
    for index, plan in enumerate(planned, 1):
        record = corrupt.get(plan["key"])
        if not record:
            continue
        lines.append(f"  window {index} of {len(planned)}: {labels[plan['key']]}, "
                     f"measured {record.get('measured_at', 'at an unrecorded time')}, "
                     f"{refusal(record) or 'never settled'}")
    lines.append(f"  re-measure them with: {command}")
    lines.append(f"  that re-measures exactly these {len(corrupt)} window"
                 f"{'s' if len(corrupt) != 1 else ''} and supersedes the corrupt records; "
                 f"every window whose counts did match is still skipped")
    return lines


def resume_lines(planned, measured, path, budget=None):
    """What is being skipped, what is left, and why a half window is not resumed."""
    remaining = [p for p in planned if p["key"] not in measured]
    lines = [f"resume: {len(measured)} of {len(planned)} windows already measured, "
             f"{len(remaining)} remaining"]
    if measured:
        lines.append(f"  checkpoints read from {path.name}")
    for index, plan in enumerate(planned, 1):
        record = measured.get(plan["key"])
        if record:
            lines.append(f"  skipping window {index} of {len(planned)}: {plan['label']}, "
                         f"measured {record.get('measured_at', 'at an unrecorded time')}")
    for index, plan in enumerate(planned, 1):
        if plan["key"] not in measured:
            lines.append(f"  to run, window {index} of {len(planned)}: {plan['label']}")
    lines.append("  a window interrupted mid-flight was never checkpointed: it is discarded "
                 "and re-measured whole, never resumed from the middle")
    if budget is not None and remaining:
        lines.append(f"  --max-windows {budget}: measuring {min(budget, len(remaining))} of the "
                     f"{len(remaining)} remaining windows, then stopping cleanly")
    return lines


def progress_line(planned, measured, label):
    """The count a probe stops on when it has not measured every window."""
    remaining = len(planned) - len(measured)
    return (f"{label}: {len(measured)} of {len(planned)} windows measured, "
            f"{remaining} remaining. No result is written from partial data; "
            f"re-run the identical command to measure the rest.")
