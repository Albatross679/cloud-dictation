"""The whole benchmark in one command: the grid, then scoring, then the probes.

Stages run in a fixed order and the first failure stops the sequence. The grid
goes first because it needs no silence: run.py records cost and duration from
each response individually, so other traffic on the account cannot affect it and
the captain can dictate for the whole of it. The probes go last because they are
the only stages that read account-level analytics, and dictation inside one of
their measurement windows corrupts that window.

Before anything runs, the plan states every stage, the request count, the
estimated cost and the range of time the captain cannot dictate. A live run then
asks for a typed confirmation, because that is the point where money starts
being spent.

Each stage is skipped when its output is already complete, so an interrupted
sequence can be re-run as-is. The probes resume per measurement window, so the
plan counts only the windows still to measure and the quiet-time estimate shrinks
as they complete. --grid-only and --probes-only run one half.

--max-windows N measures at most N more windows across the two probes and then
stops cleanly, which is how the probes are run in short idle stretches rather
than one long sitting.

    run_all.py --dry-run
    run_all.py --live
    run_all.py --live --probes-only --max-windows 2
"""

import argparse
import json
import subprocess
import sys
import time

import config as cfg
import quiet_window as quiet
import run as grid
import window_log

CONFIRMATION = "RUN LIVE"

HERE = cfg.REPO / "scripts" / "compression_bench"

# The grid stages need no silence; the probe stages are the only ones that do.
# The order is the running order: everything that can be done noisily first.
GRID_STAGE_KEYS = ["grid", "score", "report"]
PROBE_STAGE_KEYS = ["probe_billing", "probe_silence"]


class Stage:
    """One step of the sequence: what it runs, what it costs, whether it is done."""

    def __init__(self, key, title, argv, requests=0, cost=0.0, spends=False,
                 done=False, done_note="", note="", deferred=False, defer_note="",
                 quiet_range=None):
        self.key = key
        self.title = title
        self.argv = argv
        self.requests = requests
        self.cost = cost
        self.spends = spends
        self.done = done
        self.done_note = done_note
        self.note = note
        self.deferred = deferred
        self.defer_note = defer_note
        # Low and high quiet seconds for the windows this stage runs, or None for
        # a stage that needs no quiet at all.
        self.quiet_range = quiet_range


def read_jsonl(path):
    if not path.exists():
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def grid_stage(dry_run, models, speeds):
    """Stage 3, the main grid, sized from the cells still missing from its log."""
    variants = read_jsonl(cfg.VARIANTS)
    cells = [
        (variant, model_key)
        for variant in variants
        if any(abs(variant["speed"] - s) < 1e-9 for s in speeds)
        for model_key in models
    ]
    done = grid.load_done(cfg.responses_path(dry_run), dry_run)
    pending = [
        c for c in cells
        if grid.cell_key({"utt_id": c[0]["utt_id"], "speed": c[0]["speed"], "model": c[1]}) not in done
    ]
    cost = sum(
        variant["duration_s"] / 60 * cfg.usd_per_audio_minute(model_key)
        for variant, model_key in pending
    )
    return Stage(
        "grid",
        "Main grid, no quiet needed: dictate freely for this whole stage",
        ["run.py", mode_flag(dry_run)],
        requests=len(pending),
        cost=cost,
        spends=True,
        done=not pending and bool(cells),
        done_note=f"all {len(cells)} cells already in {cfg.responses_path(dry_run).name}",
        note="cost and duration come from each response, so other account traffic cannot affect it",
    )


def score_stage(dry_run):
    responses = read_jsonl(cfg.responses_path(dry_run))
    scored = 0
    results_path = cfg.results_path(dry_run)
    if results_path.exists():
        results = json.loads(results_path.read_text())
        scored = results.get("responses", 0)
    ok = sum(1 for r in responses if r.get("ok"))
    return Stage(
        "score", "Scoring, offline and free",
        ["score.py", mode_flag(dry_run)],
        done=bool(ok) and scored == ok,
        done_note=f"{results_path.name} already covers all {ok} responses",
    )


def report_stage(dry_run):
    results_path = cfg.results_path(dry_run)
    report_path = cfg.report_path(dry_run)
    fresh = (report_path.exists() and results_path.exists()
             and report_path.stat().st_mtime >= results_path.stat().st_mtime)
    return Stage(
        "report", "Report, offline and free",
        ["report.py", mode_flag(dry_run)],
        done=fresh,
        done_note=f"{report_path.name} is newer than {results_path.name}",
    )


def probe_done(path, dry_run):
    """A probe result counts as complete only when it was written in this mode."""
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text()).get("synthetic")) == dry_run
    except (json.JSONDecodeError, OSError):
        return False


def window_cost(schedule, indices, models):
    """Cost of a probe's windows, over the indices named."""
    per_minute = sum(cfg.usd_per_audio_minute(m) for m in models)
    return sum(
        schedule.windows[i].audio_seconds / len(models) / 60 * per_minute for i in indices
    )


def probe_stage(key, title_prefix, script, result_path, schedule, dry_run, models, budget):
    """One probe, sized from the windows it still has to measure.

    `budget` is how many windows this probe may measure in this run, or None for
    all of them. A probe with windows left but no budget is deferred rather than
    started, so the run stops cleanly on the count the captain asked for.
    """
    remaining = schedule.remaining_indices()
    running = remaining if budget is None else remaining[:budget]
    argv = [script, mode_flag(dry_run),
            "--window-offset", str(schedule.offset), "--window-total", str(schedule.total)]
    if budget is not None and running:
        argv += ["--max-windows", str(len(running))]
    mode_name = "dry run" if dry_run else "live run"
    ranges = [schedule.window_range(i) for i in running]
    return Stage(
        key,
        f"{title_prefix}: {len(running)} quiet window{'s' if len(running) != 1 else ''} "
        f"of {len(schedule.windows)}, do not dictate inside them",
        argv,
        requests=sum(schedule.windows[i].requests for i in running),
        cost=window_cost(schedule, running, models),
        spends=True,
        done=not remaining and probe_done(result_path, dry_run),
        done_note=f"all {len(schedule.windows)} windows are checkpointed and "
                  f"{result_path.name} holds a {mode_name}",
        deferred=bool(remaining) and not running,
        defer_note=f"{len(remaining)} window{'s' if len(remaining) != 1 else ''} left, and "
                   f"--max-windows is already spent on the earlier probe",
        note=f"{len(schedule.windows) - len(remaining)} of {len(schedule.windows)} windows "
             f"already measured and checkpointed"
             if len(remaining) != len(schedule.windows) else "",
        quiet_range=(sum(low for low, _ in ranges), sum(high for _, high in ranges)),
    )


def probe_stages(dry_run, models, schedules, max_windows=None):
    """P1 and P2, each carrying its slice of the global window numbering.

    A window budget is spent in running order: P1 takes what it needs first, and
    P2 gets whatever is left.
    """
    billing, silence = schedules
    billing_budget = max_windows
    silence_budget = None
    if max_windows is not None:
        billing_budget = min(max_windows, len(billing.remaining_indices()))
        silence_budget = max_windows - billing_budget
    return [
        probe_stage("probe_billing", "P1 billing probe", "probe_billing.py",
                    cfg.BILLING_PROBE_RESULT, billing, dry_run, models, billing_budget),
        probe_stage("probe_silence", "P2 silence probe", "probe_silence.py",
                    cfg.SILENCE_PROBE_RESULT, silence, dry_run, models, silence_budget),
    ]


def mode_flag(dry_run):
    return "--dry-run" if dry_run else "--live"


def completed_windows(result_path, dry_run, shape, keys):
    """Indices of a probe's windows that its checkpoint log already holds, and
    the settle times measured for them."""
    measured = window_log.load_windows(
        cfg.probe_windows_path(result_path, dry_run), dry_run, shape,
        cfg.probe_windows_path(result_path, not dry_run))
    indices = {i for i, key in enumerate(keys) if key in measured}
    settles = [r.get("settle_seconds_observed") for r in measured.values()]
    return indices, settles


def build_schedules(models, dry_run):
    """The two probes' windows, numbered as one sequence, with the windows their
    checkpoint logs already hold marked off."""
    variants = read_jsonl(cfg.VARIANTS)
    billing = quiet.billing_windows(cfg.BILLING_PROBE_SPEEDS, cfg.BILLING_PROBE_REPLICATES,
                                    cfg.BILLING_PROBE_UTTERANCES, models, variants)
    silence = quiet.silence_windows(cfg.SILENCE_PROBE_PADDING_S, cfg.SILENCE_PROBE_REPEATS,
                                    models, cfg.SILENCE_PROBE_SPEECH_S)
    total = len(billing) + len(silence)

    billing_keys = [window_log.billing_key(replicate, speed)
                    for replicate in range(1, cfg.BILLING_PROBE_REPLICATES + 1)
                    for speed in cfg.BILLING_PROBE_SPEEDS]
    silence_keys = [window_log.silence_key(p) for p in cfg.SILENCE_PROBE_PADDING_S]
    billing_done, billing_settles = completed_windows(
        cfg.BILLING_PROBE_RESULT, dry_run,
        window_log.billing_shape(cfg.BILLING_PROBE_UTTERANCES, models), billing_keys)
    silence_done, silence_settles = completed_windows(
        cfg.SILENCE_PROBE_RESULT, dry_run,
        window_log.silence_shape(cfg.SILENCE_PROBE_REPEATS, models, cfg.SILENCE_PROBE_SPEECH_S),
        silence_keys)

    schedules = (quiet.QuietSchedule(billing, offset=0, total=total, completed=billing_done),
                 quiet.QuietSchedule(silence, offset=len(billing), total=total,
                                     completed=silence_done))
    for schedule, settles in zip(schedules, (billing_settles, silence_settles)):
        for settle in settles:
            schedule.observe(settle)
    return schedules


def print_plan(stages, schedules, dry_run, quiet_stages):
    """Everything the captain needs before deciding, printed before anything runs."""
    print(quiet.rule("="))
    print(f"compression benchmark, {'dry run' if dry_run else 'LIVE RUN'}: "
          f"{len(stages)} stages in order")
    print(quiet.rule("="))
    for number, stage in enumerate(stages, 1):
        if stage.done:
            state = "already complete, will be skipped"
        elif stage.deferred:
            state = "left for a later run"
        else:
            state = "will run"
        print(f"\n{number}. {stage.title}")
        print(f"   command: {' '.join(stage.argv)}")
        print(f"   status: {state}")
        if stage.done and stage.done_note:
            print(f"   {stage.done_note}")
        if stage.deferred and stage.defer_note:
            print(f"   {stage.defer_note}")
        if stage.requests:
            print(f"   {stage.requests} requests, ~${stage.cost:.2f}")
        if stage.note:
            print(f"   {stage.note}")

    pending = [s for s in stages if not s.done and not s.deferred]
    print(f"\n{quiet.rule('-')}")
    print(f"requests to send: {sum(s.requests for s in pending)}")
    print(f"estimated cost: ~${sum(s.cost for s in pending):.2f}")
    if quiet_stages:
        low = high = 0.0
        windows = 0
        measured = 0
        for schedule in schedules:
            schedule_low, schedule_high = schedule.total_range()
            low += schedule_low
            high += schedule_high
            windows += len(schedule.remaining_indices())
            measured += len(schedule.completed)
        this_run = [s.quiet_range for s in pending if s.quiet_range]
        run_low = sum(r[0] for r in this_run)
        run_high = sum(r[1] for r in this_run)
        print(f"time you must not dictate in this run: {quiet.format_range(run_low, run_high)}")
        print(f"time you must not dictate to finish every probe: "
              f"{quiet.format_range(low, high)}, split across {windows} measurement "
              f"window{'s' if windows != 1 else ''} still to run")
        if measured:
            print(f"  {measured} window{'s' if measured != 1 else ''} already measured and "
                  f"checkpointed, and no longer costing quiet time")
        print(f"  {schedules[0].basis()}")
        print("  the stages before the probes need no quiet at all")
        for schedule in schedules:
            for line in schedule.plan_lines()[3:]:
                print(f"  {line}")
    else:
        print("time you must not dictate: none, no probe stage is selected")
    print(quiet.rule("-"))


def confirm(stages, dry_run):
    """Ask for the confirmation phrase before the first request that costs money.

    A dry run spends nothing and is not gated. A live run is gated on a typed
    phrase read from a terminal, so a redirected or empty stdin refuses rather
    than falling through to a yes.
    """
    if dry_run:
        return True
    paid = [s for s in stages if s.spends and not s.done and not s.deferred and s.requests]
    if not paid:
        print("\nnothing left to pay for; no confirmation needed")
        return True
    if not sys.stdin.isatty():
        print(f"\nrefusing to spend money without a typed confirmation, and stdin is not a "
              f"terminal. Run this from a terminal and type {CONFIRMATION} when asked.")
        return False
    print(f"\nThis sends {sum(s.requests for s in paid)} paid requests, "
          f"~${sum(s.cost for s in paid):.2f}.")
    try:
        answer = input(f"Type {CONFIRMATION} to start, anything else to cancel: ")
    except EOFError:
        answer = ""
    if answer.strip() != CONFIRMATION:
        print("cancelled, nothing was sent")
        return False
    return True


def run_stage(number, count, stage, python):
    """Print the phase header, then run the stage unless it is already complete."""
    print("\n")
    print(quiet.rule("="))
    print(f"PHASE {number} of {count}: {stage.title}")
    print(quiet.rule("="))
    if stage.done:
        print(f"already complete, skipping: {stage.done_note}")
        return 0
    if stage.deferred:
        print(f"left for a later run: {stage.defer_note}")
        return 0
    # -u so a stage's progress and its quiet-window banners reach the terminal as
    # they happen, including when the whole run is piped to a log.
    argv = [python, "-u", str(HERE / stage.argv[0])] + stage.argv[1:]
    # The stage writes to the same stdout as this runner, so flush first: with
    # output redirected to a file the two would otherwise interleave by buffer.
    sys.stdout.flush()
    started = time.monotonic()
    code = subprocess.call(argv, cwd=str(HERE))
    print(f"\nphase {number} finished in {quiet.format_duration(time.monotonic() - started)}, "
          f"exit {code}")
    return code


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="run every stage in dry-run mode, sending nothing and paying nothing")
    mode.add_argument("--live", action="store_true",
                      help="run every stage against the worker and pay for it")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--grid-only", action="store_true",
                       help="grid, scoring and report only, no quiet windows at all")
    scope.add_argument("--probes-only", action="store_true",
                       help="the two probes only, for running them later or overnight")
    parser.add_argument("--models", nargs="*", default=list(cfg.MODELS),
                        help="subset of models, applied to every stage")
    parser.add_argument("--plan-only", action="store_true",
                        help="print the plan and the quiet estimate, then stop")
    parser.add_argument("--max-windows", type=int, default=None,
                        help="measure at most this many more probe windows across both probes, "
                             "then stop cleanly; the rest are left for a later run")
    return parser


def selected_stage_keys(argv):
    """Stage keys a command line selects, in running order."""
    argv = list(argv)
    if "--live" not in argv:
        argv = ["--dry-run"] + argv
    args = build_parser().parse_args(argv)
    keys = []
    if not args.probes_only:
        keys += GRID_STAGE_KEYS
    if not args.grid_only:
        keys += PROBE_STAGE_KEYS
    return keys


def main():
    args = build_parser().parse_args()

    unknown = [m for m in args.models if m not in cfg.MODELS]
    if unknown:
        raise SystemExit(f"unknown models: {', '.join(unknown)}")
    if not cfg.VARIANTS.exists():
        raise SystemExit(f"missing {cfg.VARIANTS}; run prepare_corpus.py and compress.py first")
    if args.max_windows is not None and args.max_windows < 1:
        raise SystemExit("--max-windows must be at least 1")

    cfg.catalogue()
    keys = selected_stage_keys(sys.argv[1:])
    available = {}
    schedules = build_schedules(args.models, args.dry_run)
    if not args.probes_only:
        available["grid"] = grid_stage(args.dry_run, args.models, cfg.SPEEDS)
        available["score"] = score_stage(args.dry_run)
        available["report"] = report_stage(args.dry_run)
    if not args.grid_only:
        for stage in probe_stages(args.dry_run, args.models, schedules, args.max_windows):
            available[stage.key] = stage
    stages = [available[key] for key in keys]

    print_plan(stages, schedules, args.dry_run, quiet_stages=not args.grid_only)
    if args.plan_only:
        return 0
    if not confirm(stages, args.dry_run):
        return 1

    python = sys.executable
    for number, stage in enumerate(stages, 1):
        code = run_stage(number, len(stages), stage, python)
        if code != 0:
            print(f"\n{quiet.rule('=')}")
            print(f"STOPPED: phase {number} ({stage.key}) exited {code}. "
                  f"Later phases did not run.")
            print("Fix the cause and re-run this command; completed phases are skipped.")
            print(quiet.rule("="))
            return code

    print(f"\n{quiet.rule('=')}")
    left = windows_left(args.models, args.dry_run) if not args.grid_only else 0
    if left:
        print(f"stopped cleanly: {left} measurement window{'s' if left != 1 else ''} still to "
              f"measure. Every window already measured is checkpointed and will be skipped.")
        print("Re-run this command in the next idle stretch to continue.")
    else:
        print(f"all {len(stages)} phases complete. Dictation is safe from here on.")
    print(quiet.rule("="))
    return 0


def windows_left(models, dry_run):
    """Probe windows still to measure, read back from the checkpoint logs."""
    return sum(len(s.remaining_indices()) for s in build_schedules(models, dry_run))


if __name__ == "__main__":
    sys.exit(main())
