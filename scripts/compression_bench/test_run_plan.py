"""Tests for the quiet-window signalling, the quiet-time estimate and the runner.

Run them with the benchmark's own interpreter, from this directory:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_run_plan -v
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


def window(label="P1 billing, 1x", requests=10, audio_seconds=100.0):
    return quiet.Window(label, requests, audio_seconds)


class FormatDuration(unittest.TestCase):
    def test_short_spans_stay_in_seconds(self):
        self.assertEqual(quiet.format_duration(45), "45 s")

    def test_medium_spans_are_minutes(self):
        self.assertEqual(quiet.format_duration(600), "10 min")

    def test_long_spans_are_hours_and_minutes(self):
        self.assertEqual(quiet.format_duration(3600 + 3000), "1 h 50 min")

    def test_a_range_collapses_when_both_ends_read_the_same(self):
        self.assertEqual(quiet.format_range(600, 600), "10 min")
        self.assertEqual(quiet.format_range(600, 1200), "10 min to 20 min")


class Banner(unittest.TestCase):
    def render(self, schedule, which, index=0, **kwargs):
        stream = io.StringIO()
        getattr(schedule, which)(index, stream=stream, **kwargs)
        return stream.getvalue()

    def schedule(self):
        return quiet.QuietSchedule([window("first"), window("second")])

    def test_opening_says_do_not_dictate_in_the_captains_words(self):
        text = self.render(self.schedule(), "open")
        self.assertIn("DO NOT DICTATE", text)
        self.assertNotIn("analytics window open", text)

    def test_closing_says_dictation_is_safe_again(self):
        text = self.render(self.schedule(), "close", settle_seconds=120)
        self.assertIn("SAFE TO DICTATE", text)
        self.assertIn("Dictate freely", text)

    def test_the_two_blocks_are_told_apart_without_color_or_emoji(self):
        opening = self.render(self.schedule(), "open")
        closing = self.render(self.schedule(), "close")
        self.assertIn(quiet.QUIET_RULE * 20, opening)
        self.assertIn(quiet.SAFE_RULE * 20, closing)
        self.assertNotIn(quiet.QUIET_RULE, closing)
        self.assertNotIn("\x1b[", opening + closing)
        self.assertTrue((opening + closing).isascii())

    def test_a_rule_spans_the_terminal_width(self):
        text = self.render(self.schedule(), "open")
        rules = [line for line in text.splitlines()
                 if set(line) == {quiet.QUIET_RULE} and len(line) > 4]
        self.assertTrue(rules)
        for line in rules:
            self.assertEqual(len(line), quiet.terminal_width())

    def test_the_block_carries_the_window_number_and_the_count(self):
        text = self.render(self.schedule(), "open", index=1)
        self.assertIn("Window 2 of 2", text)

    def test_a_closing_block_points_at_the_next_window(self):
        text = self.render(self.schedule(), "close", index=0)
        self.assertIn("window 2 of 2", text)
        self.assertIn("1 window left", text)

    def test_the_last_closing_block_promises_nothing_further(self):
        self.assertIn("no windows left", self.render(self.schedule(), "close", index=1))


class Numbering(unittest.TestCase):
    def test_an_offset_continues_the_sequence_the_runner_started(self):
        schedule = quiet.QuietSchedule([window(), window()], offset=6, total=11)
        self.assertEqual(schedule.number(0), 7)
        self.assertEqual(schedule.total, 11)

    def test_a_standalone_probe_numbers_its_own_windows(self):
        schedule = quiet.QuietSchedule([window(), window()])
        self.assertEqual((schedule.number(0), schedule.total), (1, 2))


class Estimate(unittest.TestCase):
    def test_a_window_costs_its_send_time_plus_the_settle_bracket(self):
        schedule = quiet.QuietSchedule([window(requests=10, audio_seconds=100.0)])
        send = 10 * quiet.REQUEST_OVERHEAD_S + 100.0 * quiet.REQUEST_PER_AUDIO_SECOND_S
        settle_low = cfg.ANALYTICS_POLL_INTERVAL_S * (cfg.ANALYTICS_SETTLE_COMPLETE_READS - 1)
        low, high = schedule.window_range(0)
        self.assertAlmostEqual(low, send + max(settle_low, quiet.boundary_hold_seconds()))
        self.assertAlmostEqual(high, send + quiet.MINUTE_RESIDUAL_MAX_S
                               + max(cfg.ANALYTICS_SETTLE_TIMEOUT_S,
                                     quiet.boundary_hold_seconds()))

    def test_the_total_is_the_sum_over_every_window(self):
        schedule = quiet.QuietSchedule([window(), window(), window()])
        one_low, one_high = schedule.window_range(0)
        low, high = schedule.total_range()
        self.assertAlmostEqual(low, 3 * one_low)
        self.assertAlmostEqual(high, 3 * one_high)

    def test_remaining_time_counts_only_the_windows_still_to_come(self):
        schedule = quiet.QuietSchedule([window(), window(), window()])
        one_low, _ = schedule.window_range(0)
        self.assertAlmostEqual(schedule.range_from(2)[0], one_low)
        self.assertEqual(schedule.range_from(3), (0.0, 0.0))

    def test_a_measured_settle_replaces_the_guess(self):
        schedule = quiet.QuietSchedule([window(), window()])
        before_low, before_high = schedule.total_range()
        schedule.observe(90.0)
        schedule.observe(150.0)
        after_low, after_high = schedule.total_range()
        self.assertGreater(after_low, before_low)
        self.assertLess(after_high, before_high)
        self.assertIn("refined from 2 measured settle times", schedule.basis())

    def test_an_unmeasured_settle_is_not_recorded(self):
        schedule = quiet.QuietSchedule([window()])
        schedule.observe(None)
        self.assertEqual(schedule.observed_settles, [])
        self.assertIn("assumes a settle", schedule.basis())

    def test_the_untouched_estimate_names_the_cap_it_assumes(self):
        schedule = quiet.QuietSchedule([window()])
        self.assertIn(quiet.format_duration(cfg.ANALYTICS_SETTLE_TIMEOUT_S), schedule.basis())


class ConfiguredWindows(unittest.TestCase):
    def variants(self):
        return [{"speed": speed, "utt_id": f"u{i:03d}", "duration_s": 7.5 / speed}
                for speed in cfg.BILLING_PROBE_SPEEDS for i in range(300)]

    def test_the_probes_open_one_window_per_speed_per_replicate_and_per_padding(self):
        billing = quiet.billing_windows(cfg.BILLING_PROBE_SPEEDS, cfg.BILLING_PROBE_REPLICATES,
                                        cfg.BILLING_PROBE_UTTERANCES, list(cfg.MODELS),
                                        self.variants())
        silence = quiet.silence_windows(cfg.SILENCE_PROBE_PADDING_S, cfg.SILENCE_PROBE_REPEATS,
                                        list(cfg.MODELS), cfg.SILENCE_PROBE_SPEECH_S)
        self.assertEqual(len(billing),
                         cfg.BILLING_PROBE_REPLICATES * len(cfg.BILLING_PROBE_SPEEDS))
        self.assertEqual(len(silence), len(cfg.SILENCE_PROBE_PADDING_S))
        self.assertEqual(len(billing) + len(silence), 11)

    def test_a_billing_window_sends_every_clip_to_every_model(self):
        billing = quiet.billing_windows([1.0], 1, 200, list(cfg.MODELS), self.variants())
        self.assertEqual(billing[0].requests, 200 * len(cfg.MODELS))

    def test_a_silence_window_sends_every_repeat_to_every_model(self):
        silence = quiet.silence_windows([4], 3, list(cfg.MODELS), 62.0)
        self.assertEqual(silence[0].requests, 3 * len(cfg.MODELS))
        self.assertAlmostEqual(silence[0].audio_seconds, 66.0 * 3 * len(cfg.MODELS))


class SleepWithProgress(unittest.TestCase):
    def test_a_redirected_wait_prints_a_whole_line_straight_away(self):
        stream = io.StringIO()
        quiet.sleep_with_progress(0.05, "waiting", stream=stream, tick=0.01)
        lines = stream.getvalue().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertIn("waiting", line)
            self.assertIn("to go", line)

    def test_a_zero_wait_prints_nothing(self):
        stream = io.StringIO()
        quiet.sleep_with_progress(0, "waiting", stream=stream)
        self.assertEqual(stream.getvalue(), "")


class ProbeCompletion(unittest.TestCase):
    def test_a_result_from_the_other_mode_does_not_count_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "billing.json"
            path.write_text(json.dumps({"synthetic": True}))
            self.assertTrue(run_all.probe_done(path, dry_run=True))
            self.assertFalse(run_all.probe_done(path, dry_run=False))

    def test_a_missing_or_unreadable_result_is_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(run_all.probe_done(Path(tmp) / "nothing.json", dry_run=True))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{")
            self.assertFalse(run_all.probe_done(broken, dry_run=True))


class Confirmation(unittest.TestCase):
    def paid_stage(self, done=False):
        return run_all.Stage("probe_billing", "P1", ["probe_billing.py", "--live"],
                             requests=100, cost=1.0, spends=True, done=done)

    def confirm(self, stages, dry_run):
        """confirm(), with its prompt text kept out of the test output."""
        with contextlib.redirect_stdout(io.StringIO()):
            return run_all.confirm(stages, dry_run)

    def test_a_dry_run_is_not_gated(self):
        self.assertTrue(self.confirm([self.paid_stage()], dry_run=True))

    def test_a_non_terminal_stdin_refuses_rather_than_assuming_yes(self):
        with mock.patch("sys.stdin", io.StringIO(run_all.CONFIRMATION + "\n")):
            self.assertFalse(self.confirm([self.paid_stage()], dry_run=False))

    def test_the_exact_phrase_is_required(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            for answer in ("", "y", "yes", "run live", "RUN  LIVE"):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertFalse(self.confirm([self.paid_stage()], dry_run=False))
            with mock.patch("builtins.input", return_value=f"  {run_all.CONFIRMATION}  "):
                self.assertTrue(self.confirm([self.paid_stage()], dry_run=False))

    def test_an_end_of_input_is_a_refusal(self):
        with mock.patch("sys.stdin.isatty", return_value=True):
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertFalse(self.confirm([self.paid_stage()], dry_run=False))

    def test_nothing_left_to_pay_for_needs_no_confirmation(self):
        self.assertTrue(self.confirm([self.paid_stage(done=True)], dry_run=False))


class StageSelection(unittest.TestCase):
    def parse(self, argv):
        return run_all.selected_stage_keys(argv)

    def test_the_default_sequence_runs_the_grid_before_the_probes(self):
        self.assertEqual(self.parse([]),
                         ["grid", "score", "report", "probe_billing", "probe_silence"])

    def test_grid_only_leaves_out_every_quiet_window(self):
        self.assertEqual(self.parse(["--grid-only"]), ["grid", "score", "report"])

    def test_probes_only_runs_the_two_probes(self):
        self.assertEqual(self.parse(["--probes-only"]), ["probe_billing", "probe_silence"])


if __name__ == "__main__":
    unittest.main()
