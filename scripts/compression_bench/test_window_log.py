"""Tests for the probes' per-window checkpoints and their resume.

Run them with the benchmark's own interpreter, from this directory:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_window_log -v
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config as cfg
import quiet_window as quiet
import run_all
import window_log

SHAPE = window_log.billing_shape(200, ["nova-3", "whisper"])


def checkpoint(key, synthetic=True, shape=None, measured_at="2026-08-12T09:00:00Z", settle=90.0):
    return {
        "probe": "billing",
        "synthetic": synthetic,
        "window_key": key,
        "window_shape": SHAPE if shape is None else shape,
        "measured_at": measured_at,
        "settle_seconds_observed": settle,
        "models": [],
    }


def write_log(directory, name, records):
    path = Path(directory) / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def other(path):
    return path.with_name("counterpart.jsonl")


class Keys(unittest.TestCase):
    def test_a_billing_key_carries_the_replicate_and_the_speed(self):
        self.assertNotEqual(window_log.billing_key(1, 1.0), window_log.billing_key(1, 3.0))
        self.assertNotEqual(window_log.billing_key(1, 1.0), window_log.billing_key(2, 1.0))

    def test_a_speed_is_keyed_the_same_however_it_is_written(self):
        self.assertEqual(window_log.billing_key(1, 3), window_log.billing_key(1, 3.0))
        self.assertEqual(window_log.silence_key(4), window_log.silence_key(4.0))

    def test_paddings_are_keyed_apart(self):
        keys = {window_log.silence_key(p) for p in cfg.SILENCE_PROBE_PADDING_S}
        self.assertEqual(len(keys), len(cfg.SILENCE_PROBE_PADDING_S))


class LoadWindows(unittest.TestCase):
    def test_a_missing_log_is_no_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "billing.windows.dry-run.jsonl"
            self.assertEqual(window_log.load_windows(path, True, SHAPE, other(path)), {})

    def test_completed_windows_come_back_keyed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.dry-run.jsonl",
                             [checkpoint("replicate1|1x"), checkpoint("replicate1|3x")])
            measured = window_log.load_windows(path, True, SHAPE, other(path))
            self.assertEqual(set(measured), {"replicate1|1x", "replicate1|3x"})

    def test_a_dry_run_checkpoint_never_satisfies_a_live_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.jsonl", [checkpoint("replicate1|1x")])
            with self.assertRaises(SystemExit) as caught:
                window_log.load_windows(path, False, SHAPE, other(path))
            self.assertIn("dry run", str(caught.exception))
            self.assertIn(str(path), str(caught.exception))

    def test_a_live_checkpoint_never_satisfies_a_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.dry-run.jsonl",
                             [checkpoint("replicate1|1x", synthetic=False)])
            with self.assertRaises(SystemExit):
                window_log.load_windows(path, True, SHAPE, other(path))

    def test_a_mixed_log_is_refused_by_both_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.jsonl", [
                checkpoint("replicate1|1x", synthetic=True),
                checkpoint("replicate1|3x", synthetic=False),
            ])
            for dry_run in (True, False):
                with self.assertRaises(SystemExit) as caught:
                    window_log.load_windows(path, dry_run, SHAPE, other(path))
                self.assertIn("mixed", str(caught.exception))

    def test_a_window_from_a_different_batch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.dry-run.jsonl", [
                checkpoint("replicate1|1x",
                           shape=window_log.billing_shape(50, ["nova-3", "whisper"])),
            ])
            with self.assertRaises(SystemExit) as caught:
                window_log.load_windows(path, True, SHAPE, other(path))
            self.assertIn("different batch", str(caught.exception))

    def test_a_line_torn_by_a_kill_is_dropped_rather_than_half_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.dry-run.jsonl", [checkpoint("replicate1|1x")])
            with open(path, "a") as handle:
                handle.write(json.dumps(checkpoint("replicate1|3x"))[:120])
            measured = window_log.load_windows(path, True, SHAPE, other(path))
            self.assertEqual(set(measured), {"replicate1|1x"})

    def test_a_re_measured_window_replaces_the_earlier_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "billing.windows.dry-run.jsonl", [
                checkpoint("replicate1|1x", measured_at="2026-08-12T09:00:00Z"),
                checkpoint("replicate1|1x", measured_at="2026-08-13T09:00:00Z"),
            ])
            measured = window_log.load_windows(path, True, SHAPE, other(path))
            self.assertEqual(measured["replicate1|1x"]["measured_at"], "2026-08-13T09:00:00Z")


class AppendWindow(unittest.TestCase):
    def test_a_window_is_readable_the_moment_it_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probes" / "billing.windows.dry-run.jsonl"
            window_log.append_window(path, checkpoint("replicate1|1x"))
            self.assertEqual(set(window_log.load_windows(path, True, SHAPE, other(path))),
                             {"replicate1|1x"})
            window_log.append_window(path, checkpoint("replicate1|3x"))
            self.assertEqual(len(window_log.load_windows(path, True, SHAPE, other(path))), 2)


class ModePaths(unittest.TestCase):
    def test_each_mode_owns_its_own_checkpoint_file(self):
        for result in (cfg.BILLING_PROBE_RESULT, cfg.SILENCE_PROBE_RESULT):
            live = cfg.probe_windows_path(result, False)
            dry = cfg.probe_windows_path(result, True)
            self.assertNotEqual(live, dry)
            self.assertIn("dry-run", dry.name)
            self.assertNotIn("dry-run", live.name)
            self.assertEqual(live.parent, result.parent)


class MeasurementSpan(unittest.TestCase):
    def test_a_run_split_across_days_shows_the_gap(self):
        span = window_log.measurement_span(
            ["2026-08-14T09:00:00Z", "2026-08-11T09:00:00Z", "2026-08-12T21:00:00Z"])
        self.assertEqual((span["first"], span["last"]),
                         ("2026-08-11T09:00:00Z", "2026-08-14T09:00:00Z"))
        self.assertAlmostEqual(span["days"], 3.0)

    def test_no_timestamps_is_no_span(self):
        self.assertIsNone(window_log.measurement_span([]))
        self.assertIsNone(window_log.measurement_span([None]))


class ResumeLines(unittest.TestCase):
    def planned(self):
        return [{"key": f"k{i}", "label": f"window {i}"} for i in range(1, 5)]

    def lines(self, measured_keys, budget=None):
        measured = {key: checkpoint(key) for key in measured_keys}
        return "\n".join(window_log.resume_lines(
            self.planned(), measured, Path("billing.windows.dry-run.jsonl"), budget))

    def test_it_names_what_is_skipped_and_what_is_left(self):
        text = self.lines(["k1", "k2"])
        self.assertIn("2 of 4 windows already measured, 2 remaining", text)
        self.assertIn("skipping window 1 of 4: window 1", text)
        self.assertIn("to run, window 3 of 4: window 3", text)
        self.assertNotIn("skipping window 3", text)

    def test_it_says_a_half_window_is_never_resumed(self):
        self.assertIn("discarded and re-measured whole", self.lines([]))

    def test_a_budget_says_how_many_of_the_remaining_it_will_measure(self):
        self.assertIn("measuring 2 of the 4 remaining windows", self.lines([], budget=2))
        self.assertIn("measuring 3 of the 3 remaining windows", self.lines(["k1"], budget=9))

    def test_a_skipped_window_reports_when_it_was_measured(self):
        self.assertIn("measured 2026-08-12T09:00:00Z", self.lines(["k1"]))


class ProgressLine(unittest.TestCase):
    def test_it_reports_the_count_and_refuses_a_partial_result(self):
        planned = [{"key": f"k{i}", "label": str(i)} for i in range(11)]
        text = window_log.progress_line(planned, {f"k{i}": {} for i in range(8)}, "P1")
        self.assertIn("8 of 11 windows measured, 3 remaining", text)
        self.assertIn("No result is written from partial data", text)


class ScheduleWithCompletedWindows(unittest.TestCase):
    def schedule(self, completed=()):
        return quiet.QuietSchedule([quiet.Window(f"w{i}", 10, 100.0) for i in range(4)],
                                   completed=completed)

    def test_a_measured_window_costs_no_more_quiet_time(self):
        full = self.schedule().total_range()
        partial = self.schedule(completed={0, 1}).total_range()
        one = self.schedule().window_range(0)
        self.assertAlmostEqual(partial[0], full[0] - 2 * one[0])
        self.assertAlmostEqual(partial[1], full[1] - 2 * one[1])

    def test_marking_a_window_done_shrinks_the_estimate_as_the_run_goes(self):
        schedule = self.schedule()
        before = schedule.total_range()
        schedule.mark_done(0)
        self.assertLess(schedule.total_range()[0], before[0])
        self.assertEqual(schedule.remaining_indices(), [1, 2, 3])

    def test_the_plan_keeps_the_numbering_and_marks_what_is_measured(self):
        lines = self.schedule(completed={0, 2}).plan_lines()
        self.assertIn("2 measurement windows still to run", lines[0])
        self.assertIn("2 of 4 already measured", lines[0])
        self.assertIn("window 1 of 4: w0, already measured", lines[3])
        self.assertIn("window 2 of 4: w1,", lines[4])
        self.assertNotIn("already measured", lines[4])

    def test_a_closing_block_points_past_the_windows_already_measured(self):
        schedule = self.schedule(completed={1, 2})
        schedule.mark_done(0)
        import io
        stream = io.StringIO()
        schedule.close(0, stream=stream)
        text = stream.getvalue()
        self.assertIn("1 window left", text)
        self.assertIn("window 4 of 4 is announced", text)


class RunnerPlan(unittest.TestCase):
    """The runner's plan and its window budget, over synthetic schedules."""

    def schedules(self, billing_done=(), silence_done=()):
        billing = quiet.QuietSchedule([quiet.Window(f"P1 w{i}", 10, 100.0) for i in range(6)],
                                      offset=0, total=11, completed=billing_done)
        silence = quiet.QuietSchedule([quiet.Window(f"P2 w{i}", 4, 200.0) for i in range(5)],
                                      offset=6, total=11, completed=silence_done)
        return billing, silence

    def stages(self, billing_done=(), silence_done=(), max_windows=None, result_written=False):
        """The two probe stages, with the result files and the worker's rates stood in for."""
        with mock.patch.object(run_all, "probe_done", return_value=result_written), \
                mock.patch.object(cfg, "usd_per_audio_minute", return_value=0.01):
            return run_all.probe_stages(True, list(cfg.MODELS),
                                        self.schedules(billing_done, silence_done), max_windows)

    def test_the_plan_counts_only_the_windows_that_remain(self):
        billing, _ = self.stages(billing_done={0, 1, 2})
        self.assertIn("3 quiet windows of 6", billing.title)
        self.assertEqual(billing.requests, 30)
        self.assertIn("3 of 6 windows already measured", billing.note)

    def test_a_probe_with_every_window_measured_still_needs_its_result_file(self):
        self.assertFalse(self.stages(billing_done=set(range(6)))[0].done)
        self.assertTrue(self.stages(billing_done=set(range(6)), result_written=True)[0].done)

    def test_a_probe_with_windows_left_is_never_complete(self):
        self.assertFalse(self.stages(billing_done={0, 1}, result_written=True)[0].done)

    def test_a_budget_is_spent_on_the_first_probe_and_the_second_is_deferred(self):
        billing, silence = self.stages(max_windows=2)
        self.assertIn("--max-windows", billing.argv)
        self.assertEqual(billing.argv[billing.argv.index("--max-windows") + 1], "2")
        self.assertEqual(billing.requests, 20)
        self.assertTrue(silence.deferred)
        self.assertEqual(silence.requests, 0)
        self.assertIn("5 windows left", silence.defer_note)

    def test_a_budget_spills_into_the_second_probe_when_the_first_is_done(self):
        billing, silence = self.stages(billing_done=set(range(6)), max_windows=3)
        self.assertNotIn("--max-windows", billing.argv)
        self.assertEqual(billing.requests, 0)
        self.assertFalse(silence.deferred)
        self.assertEqual(silence.argv[silence.argv.index("--max-windows") + 1], "3")

    def test_no_budget_runs_every_remaining_window(self):
        billing, silence = self.stages()
        for stage in (billing, silence):
            self.assertNotIn("--max-windows", stage.argv)
            self.assertFalse(stage.deferred)

    def test_a_deferred_stage_is_not_run_and_is_not_a_failure(self):
        _, silence = self.stages(max_windows=1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_all.run_stage(2, 2, silence, "python"), 0)
        self.assertIn("left for a later run", out.getvalue())

    def test_a_deferred_stage_is_nothing_to_confirm_spending_on(self):
        _, silence = self.stages(max_windows=1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(run_all.confirm([silence], dry_run=False))

    def test_the_window_numbering_still_spans_both_probes(self):
        billing, silence = self.stages(billing_done={0, 1})
        self.assertEqual(billing.argv[billing.argv.index("--window-total") + 1], "11")
        self.assertEqual(silence.argv[silence.argv.index("--window-offset") + 1], "6")


if __name__ == "__main__":
    unittest.main()
