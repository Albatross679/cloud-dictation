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
sequence can be re-run as-is. --grid-only and --probes-only run one half.

    run_all.py --dry-run
    run_all.py --live
"""

import argparse
import json
import subprocess
import sys
import time

import config as cfg
import quiet_window as quiet
import run as grid

CONFIRMATION = "RUN LIVE"

HERE = cfg.REPO / "scripts" / "compression_bench"

# The grid stages need no silence; the probe stages are the only ones that do.
# The order is the running order: everything that can be done noisily first.
GRID_STAGE_KEYS = ["grid", "score", "report"]
PROBE_STAGE_KEYS = ["probe_billing", "probe_silence"]


class Stage:
    """One step of the sequence: what it runs, what it costs, whether it is done."""

    def __init__(self, key, title, argv, requests=0, cost=0.0, spends=False,
                 done=False, done_note="", note=""):
        self.key = key
        self.title = title
        self.argv = argv
        self.requests = requests
        self.cost = cost
        self.spends = spends
        self.done = done
        self.done_note = done_note
        self.note = note


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
        variant["duration_s"] / 60 * cfg.MODELS[model_key]["neurons_per_audio_minute"]
        for variant, model_key in pending
    ) * cfg.USD_PER_1000_NEURONS / 1000
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


def probe_stages(dry_run, models, schedules):
    """P1 and P2, each carrying its slice of the global window numbering."""
    billing, silence = schedules
    total = len(billing.windows) + len(silence.windows)
    billing_requests = sum(w.requests for w in billing.windows)
    silence_requests = sum(w.requests for w in silence.windows)
    billing_cost = sum(
        w.audio_seconds / len(models) / 60 * sum(
            cfg.MODELS[m]["neurons_per_audio_minute"] for m in models)
        for w in billing.windows
    ) * cfg.USD_PER_1000_NEURONS / 1000
    silence_cost = sum(
        w.audio_seconds / len(models) / 60 * sum(
            cfg.MODELS[m]["neurons_per_audio_minute"] for m in models)
        for w in silence.windows
    ) * cfg.USD_PER_1000_NEURONS / 1000
    return [
        Stage(
            "probe_billing",
            f"P1 billing probe: {len(billing.windows)} quiet windows, do not dictate inside them",
            ["probe_billing.py", mode_flag(dry_run),
             "--window-offset", "0", "--window-total", str(total)],
            requests=billing_requests,
            cost=billing_cost,
            spends=True,
            done=probe_done(cfg.BILLING_PROBE_RESULT, dry_run),
            done_note=f"{cfg.BILLING_PROBE_RESULT.name} already holds a "
                      f"{'dry run' if dry_run else 'live run'}",
        ),
        Stage(
            "probe_silence",
            f"P2 silence probe: {len(silence.windows)} quiet windows, do not dictate inside them",
            ["probe_silence.py", mode_flag(dry_run),
             "--window-offset", str(len(billing.windows)), "--window-total", str(total)],
            requests=silence_requests,
            cost=silence_cost,
            spends=True,
            done=probe_done(cfg.SILENCE_PROBE_RESULT, dry_run),
            done_note=f"{cfg.SILENCE_PROBE_RESULT.name} already holds a "
                      f"{'dry run' if dry_run else 'live run'}",
        ),
    ]


def mode_flag(dry_run):
    return "--dry-run" if dry_run else "--live"


def build_schedules(models):
    """The two probes' windows, numbered as one sequence."""
    variants = read_jsonl(cfg.VARIANTS)
    billing = quiet.billing_windows(cfg.BILLING_PROBE_SPEEDS, cfg.BILLING_PROBE_REPLICATES,
                                    cfg.BILLING_PROBE_UTTERANCES, models, variants)
    silence = quiet.silence_windows(cfg.SILENCE_PROBE_PADDING_S, cfg.SILENCE_PROBE_REPEATS,
                                    models, cfg.SILENCE_PROBE_SPEECH_S)
    total = len(billing) + len(silence)
    return (quiet.QuietSchedule(billing, offset=0, total=total),
            quiet.QuietSchedule(silence, offset=len(billing), total=total))


def print_plan(stages, schedules, dry_run, quiet_stages):
    """Everything the captain needs before deciding, printed before anything runs."""
    print(quiet.rule("="))
    print(f"compression benchmark, {'dry run' if dry_run else 'LIVE RUN'}: "
          f"{len(stages)} stages in order")
    print(quiet.rule("="))
    for number, stage in enumerate(stages, 1):
        state = "already complete, will be skipped" if stage.done else "will run"
        print(f"\n{number}. {stage.title}")
        print(f"   command: {' '.join(stage.argv)}")
        print(f"   status: {state}")
        if stage.done and stage.done_note:
            print(f"   {stage.done_note}")
        if stage.requests:
            print(f"   {stage.requests} requests, ~${stage.cost:.2f}")
        if stage.note:
            print(f"   {stage.note}")

    pending = [s for s in stages if not s.done]
    print(f"\n{quiet.rule('-')}")
    print(f"requests to send: {sum(s.requests for s in pending)}")
    print(f"estimated cost: ~${sum(s.cost for s in pending):.2f}")
    if quiet_stages:
        low = high = 0.0
        windows = 0
        for schedule in schedules:
            schedule_low, schedule_high = schedule.total_range()
            low += schedule_low
            high += schedule_high
            windows += len(schedule.windows)
        print(f"time you must not dictate: {quiet.format_range(low, high)}, "
              f"split across {windows} measurement windows")
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
    paid = [s for s in stages if s.spends and not s.done and s.requests]
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

    schedules = build_schedules(args.models)
    keys = selected_stage_keys(sys.argv[1:])
    available = {}
    if not args.probes_only:
        available["grid"] = grid_stage(args.dry_run, args.models, cfg.SPEEDS)
        available["score"] = score_stage(args.dry_run)
        available["report"] = report_stage(args.dry_run)
    if not args.grid_only:
        for stage in probe_stages(args.dry_run, args.models, schedules):
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
    print(f"all {len(stages)} phases complete. Dictation is safe from here on.")
    print(quiet.rule("="))
    return 0


if __name__ == "__main__":
    sys.exit(main())
