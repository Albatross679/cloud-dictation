"""The quiet windows the probes need, signalled loudly enough to see across a room.

Only the probes need silence, and only inside a measurement window. They read
account-level analytics filtered to the four speech models and the Workers
binding request source, which is the exact path the captain's own dictation
takes, so a single dictated phrase inside a window lands in that window's totals.
The main grid records cost and duration per response and is unaffected by other
traffic on the account.

This module owns three things: the two banners that mark a window opening and
closing, a sleep that keeps printing while it waits so a minute-granularity poll
never looks frozen, and the arithmetic that turns the configured window count
into an honest range for how long the captain cannot dictate. The range is
recomputed from real settle times as soon as any window has been measured.

Banners use the full terminal width, blank lines and a rule made of characters,
so they survive a terminal with no color and no emoji support.
"""

import shutil
import sys
import time

import config as cfg

# Wall clock one serialized probe request takes, split into a fixed part and a
# part that scales with the audio in the clip. These match the shape the worker
# reports in transcribe_ms plus the round trip, and they are the only estimate
# in the quiet-time arithmetic that is not read straight from config.
REQUEST_OVERHEAD_S = 0.30
REQUEST_PER_AUDIO_SECOND_S = 0.09

# A window closes on a minute boundary, so the probe holds for whatever is left of
# the current minute before it may read the window back. That wait is between
# nothing and a whole minute.
MINUTE_RESIDUAL_MAX_S = 60.0

# Floor on the gap held between one window closing and the next one opening,
# measured from the closing window's end. A live P1 run saw a window's analytics
# still arriving 129 s after it closed while its neighbours were opened a minute
# apart, which counted one window's traffic inside another's range. The floor sits
# comfortably above that worst observed lag.
BOUNDARY_HOLD_FLOOR_S = 180.0

# Multiple of the worst settle measured so far, used as the gap once any window has
# been measured. Settles are timed from a window's own end and never include the
# gap, so the two do not feed each other.
BOUNDARY_HOLD_SETTLE_MULTIPLE = 1.5


def boundary_hold_seconds(observed_settles=()):
    """Seconds one window's end and the next window's start must be apart."""
    observed = [float(s) for s in observed_settles if s is not None]
    if observed:
        return max(BOUNDARY_HOLD_FLOOR_S, BOUNDARY_HOLD_SETTLE_MULTIPLE * max(observed))
    return BOUNDARY_HOLD_FLOOR_S


QUIET_RULE = "#"
SAFE_RULE = "-"

SPINNER = "|/-\\"


def terminal_width(default=80):
    """Columns available, clamped so a banner stays readable in either extreme."""
    width = shutil.get_terminal_size((default, 24)).columns
    return max(40, min(width, 120))


def rule(char):
    return char * terminal_width()


def format_duration(seconds):
    """Seconds as the captain reads them: 45 s, 8 min, 2 h 05 min."""
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours, remainder = divmod(int(round(minutes)), 60)
    return f"{hours} h {remainder:02d} min"


def format_range(low, high):
    """A low-to-high span, collapsed when both ends round to the same reading."""
    low_text = format_duration(low)
    high_text = format_duration(high)
    if low_text == high_text:
        return low_text
    return f"{low_text} to {high_text}"


def banner(headline, lines, char, stream=None):
    """A full-width block: blank space, rules, the headline, then the detail."""
    stream = stream or sys.stdout
    width = terminal_width()
    bar = char * width
    stream.write("\n\n")
    stream.write(bar + "\n")
    stream.write(bar + "\n")
    stream.write(f"{char}{char}  {headline}\n")
    stream.write(f"{char}{char}\n")
    for line in lines:
        stream.write(f"{char}{char}  {line}\n" if line else f"{char}{char}\n")
    stream.write(bar + "\n")
    stream.write(bar + "\n")
    stream.write("\n")
    stream.flush()


def sleep_with_progress(seconds, label, stream=None, tick=1.0):
    """Sleep, printing an advancing line the whole time so nothing looks wedged.

    On a terminal the line is rewritten in place once a second. When output is
    redirected it prints a fresh line every 15 s instead, so a log stays legible.
    """
    stream = stream or sys.stdout
    if seconds <= 0:
        return
    interactive = hasattr(stream, "isatty") and stream.isatty()
    deadline = time.monotonic() + seconds
    frame = 0
    last_written = -999.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        elapsed = seconds - remaining
        if interactive:
            stream.write(f"\r    {SPINNER[frame % len(SPINNER)]} {label}, "
                         f"{format_duration(remaining)} to go   ")
            stream.flush()
        elif elapsed - last_written >= 15:
            stream.write(f"    {label}, {format_duration(remaining)} to go\n")
            stream.flush()
            last_written = elapsed
        frame += 1
        time.sleep(min(tick, remaining))
    if interactive:
        stream.write("\r" + " " * (terminal_width() - 1) + "\r")
        stream.flush()


class Window:
    """One measurement window: what it sends, and what it is called out loud."""

    def __init__(self, label, requests, audio_seconds):
        self.label = label
        self.requests = requests
        self.audio_seconds = audio_seconds

    @property
    def send_seconds(self):
        return self.requests * REQUEST_OVERHEAD_S + self.audio_seconds * REQUEST_PER_AUDIO_SECOND_S


def billing_windows(speeds, replicates, clips, models, variants):
    """The windows probe_billing opens, one per speed per replicate."""
    windows = []
    for index in range(1, replicates + 1):
        for speed in speeds:
            picked = sorted(
                (v for v in variants if abs(v["speed"] - speed) < 1e-9),
                key=lambda v: v["utt_id"],
            )[:clips]
            audio = sum(v["duration_s"] for v in picked) * len(models)
            windows.append(Window(
                f"P1 billing, {speed:g}x, replicate {index} of {replicates}",
                len(picked) * len(models),
                audio,
            ))
    return windows


def silence_windows(paddings, repeats, models, speech_seconds):
    """The windows probe_silence opens, one per padding."""
    windows = []
    for padding in paddings:
        requests = len(models) * repeats
        windows.append(Window(
            f"P2 silence, {padding:g} s padding",
            requests,
            (speech_seconds + padding) * requests,
        ))
    return windows


class QuietSchedule:
    """Every window of a run, numbered globally, with the quiet-time estimate.

    `offset` and `total` let a probe number its windows inside a longer sequence
    the runner drives, so the captain sees "window 8 of 11" rather than a count
    that restarts at each probe.

    `completed` holds the indices of windows already checkpointed. They keep
    their numbers, so the sequence still reads the same, but the quiet-time
    estimate counts only the windows still to run.
    """

    def __init__(self, windows, offset=0, total=None, completed=None):
        self.windows = list(windows)
        self.offset = offset
        self.total = total if total is not None else offset + len(self.windows)
        self.observed_settles = []
        self.completed = set(completed or ())

    def number(self, index):
        """Human window number, 1 based, in the full sequence."""
        return self.offset + index + 1

    def mark_done(self, index):
        """Record a window as measured, so it drops out of the estimate."""
        self.completed.add(index)

    def remaining_indices(self):
        """Indices of the windows still to be measured, in running order."""
        return [i for i in range(len(self.windows)) if i not in self.completed]

    def settle_bracket(self):
        """Low and high settle seconds per window, measured when anything has been."""
        if self.observed_settles:
            return min(self.observed_settles), max(self.observed_settles)
        return (cfg.ANALYTICS_POLL_INTERVAL_S * (cfg.ANALYTICS_SETTLE_COMPLETE_READS - 1),
                float(cfg.ANALYTICS_SETTLE_TIMEOUT_S))

    def window_range(self, index):
        """Low and high quiet seconds for one window.

        A window costs its send, whatever is left of its last minute, and then the
        longer of its settle and the gap held before the next window may open.
        """
        settle_low, settle_high = self.settle_bracket()
        hold = boundary_hold_seconds(self.observed_settles)
        send = self.windows[index].send_seconds
        return (send + max(settle_low, hold),
                send + MINUTE_RESIDUAL_MAX_S + max(settle_high, hold))

    def range_from(self, index):
        """Low and high quiet seconds over the windows from `index` onwards.

        Windows already measured cost nothing further and are left out.
        """
        low = high = 0.0
        for i in range(index, len(self.windows)):
            if i in self.completed:
                continue
            window_low, window_high = self.window_range(i)
            low += window_low
            high += window_high
        return low, high

    def total_range(self):
        return self.range_from(0)

    def observe(self, settle_seconds):
        """Record a real settle time so later estimates stop quoting the guess."""
        if settle_seconds is not None:
            self.observed_settles.append(float(settle_seconds))

    def basis(self):
        """Where the current estimate comes from, said plainly."""
        if self.observed_settles:
            measured = len(self.observed_settles)
            return (f"the estimate is refined from {measured} measured settle "
                    f"{'time' if measured == 1 else 'times'}")
        return (f"the estimate assumes a settle between "
                f"{format_duration(cfg.ANALYTICS_POLL_INTERVAL_S * (cfg.ANALYTICS_SETTLE_COMPLETE_READS - 1))} "
                f"and the {format_duration(cfg.ANALYTICS_SETTLE_TIMEOUT_S)} cap")

    # Lines plan_lines() writes before the per-window listing starts.
    HEADER_LINES = 4

    def plan_lines(self):
        """The quiet part of the plan the runner prints before spending anything."""
        low, high = self.total_range()
        remaining = self.remaining_indices()
        measured = len(self.windows) - len(remaining)
        headline = f"{len(remaining)} measurement windows still to run"
        if measured:
            headline += f", {measured} of {len(self.windows)} already measured"
        # print_plan reprints everything from HEADER_LINES on, so a line added to
        # the block above has to move that count with it.
        lines = [
            f"{headline}, {sum(self.windows[i].requests for i in remaining)} requests inside them",
            f"do not dictate for {format_range(low, high)} in total, split across those windows",
            self.basis(),
            f"consecutive windows are held "
            f"{format_duration(boundary_hold_seconds(self.observed_settles))} apart, so one "
            f"window's analytics lag cannot land inside the next window's range",
        ]
        for index, window in enumerate(self.windows):
            if index in self.completed:
                lines.append(f"  window {self.number(index)} of {self.total}: {window.label}, "
                             f"already measured, no quiet needed")
                continue
            window_low, window_high = self.window_range(index)
            lines.append(f"  window {self.number(index)} of {self.total}: {window.label}, "
                         f"{format_range(window_low, window_high)}")
        return lines

    def open(self, index, stream=None):
        """The block printed immediately before a window opens."""
        window = self.windows[index]
        low, high = self.window_range(index)
        banner(
            "DO NOT DICTATE",
            [
                f"Window {self.number(index)} of {self.total} is opening now: {window.label}.",
                "",
                "Anything you dictate from now until this window closes is billed",
                "through the same worker and lands inside this measurement. It would",
                "silently corrupt the result.",
                "",
                f"This window lasts {format_range(low, high)}; {self.basis()}.",
            ],
            QUIET_RULE,
            stream=stream,
        )

    def close(self, index, settle_seconds=None, stream=None):
        """The block printed once a window has closed and its settle has finished."""
        window = self.windows[index]
        remaining_windows = len([i for i in self.remaining_indices() if i > index])
        lines = [
            f"Window {self.number(index)} of {self.total} is closed and settled: {window.label}.",
            "",
            "Dictate freely. Nothing is being measured right now.",
        ]
        if settle_seconds is not None:
            lines.append(f"Analytics settled {format_duration(settle_seconds)} after the window closed.")
        if remaining_windows:
            low, high = self.range_from(index + 1)
            next_index = next(i for i in self.remaining_indices() if i > index)
            lines += [
                "",
                f"{remaining_windows} window{'s' if remaining_windows != 1 else ''} left in this "
                f"probe, {format_range(low, high)} of quiet still to come.",
                f"Stop dictating when window {self.number(next_index)} of {self.total} is announced.",
                f"{self.basis()[0].upper()}{self.basis()[1:]}.",
            ]
        else:
            lines += ["", "This probe has no windows left."]
        banner("SAFE TO DICTATE", lines, SAFE_RULE, stream=stream)
