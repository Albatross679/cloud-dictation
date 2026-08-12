"""Tests for the settle rule, the boundary hold, and the recovery from corrupt windows.

These cover the defect a live P1 run exposed on 2026-08-12: four windows were
checkpointed as measurements while the analytics were still arriving, or while they
carried a neighbouring window's traffic, and one of them would have been published
as nova-3 billing 1.57x what was sent at 3x.

Run them with the benchmark's own interpreter, from this directory:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_probe_settle -v
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config as cfg
import probe_billing as probe
import quiet_window as quiet
import window_log

# The worst analytics lag the live run observed, in the one window that waited it
# out. Every gap the harness holds has to clear it.
OBSERVED_WORST_LAG_S = 128.9


def totals(**per_model):
    """An analytics read, in the shape read_window folds groups into."""
    return {
        model_key: {"requests": requests, "audio_seconds": 10.0 * requests,
                    "neurons": 0.0, "inference_ms": 100.0 * requests}
        for model_key, requests in per_model.items()
    }


def settle(reads, expected, timeout_s=300):
    """Drive settled_window over a canned sequence of analytics reads.

    The last read repeats for as long as the loop asks for another one, so a
    sequence that never completes runs to the timeout instead of the end of a list.
    The clock advances one poll interval per look, so a settle that has to time out
    does it in as many polls as it would take, and in no real time at all.
    """
    served = []
    ticks = []

    def next_read(session, start, end, models):
        served.append(len(served))
        return reads[min(len(served) - 1, len(reads) - 1)]

    def clock():
        ticks.append(len(ticks))
        return (len(ticks) - 1) * cfg.ANALYTICS_POLL_INTERVAL_S

    end = datetime.now(timezone.utc)
    with mock.patch.object(probe, "read_window", side_effect=next_read), \
            mock.patch.object(probe.time, "monotonic", side_effect=clock), \
            mock.patch.object(probe.quiet, "sleep_with_progress"), \
            mock.patch.object(cfg, "ANALYTICS_SETTLE_TIMEOUT_S", timeout_s), \
            mock.patch("builtins.print"):
        return probe.settled_window(None, end - timedelta(minutes=1), end,
                                    list(expected), expected)


class CompletenessSettle(unittest.TestCase):
    """The settle is on every request being billed, not on the reads holding still."""

    def test_a_window_short_of_its_own_traffic_is_not_a_result(self):
        short = totals(**{"nova-3": 50, "whisper-tiny-en": 3})
        result = settle([short], {"nova-3": 50, "whisper-tiny-en": 50}, timeout_s=0)
        self.assertFalse(result.complete)
        self.assertIsNone(result.lag_seconds)
        self.assertEqual(result.missing,
                         [{"model": "whisper-tiny-en", "requests_sent": 50,
                           "requests_billed": 3, "delta": -47}])

    def test_a_stable_but_incomplete_read_never_satisfies_the_settle(self):
        """The read that broke the live run: unchanging, and 3 of 50 for one model.

        Two identical reads were the old rule's whole test, and this pair passes it.
        """
        stuck = totals(**{"nova-3": 50, "whisper-tiny-en": 3})
        result = settle([stuck, stuck, stuck, stuck],
                        {"nova-3": 50, "whisper-tiny-en": 50})
        self.assertFalse(result.complete)
        self.assertGreater(result.polls, 1)
        self.assertEqual([m["delta"] for m in result.missing], [-47])

    def test_a_count_above_what_was_sent_is_refused_as_well(self):
        """59 of 50 is another window's traffic, not this window's measurement."""
        over = totals(**{"nova-3": 59})
        result = settle([over], {"nova-3": 50}, timeout_s=0)
        self.assertFalse(result.complete)
        self.assertEqual(result.missing[0]["delta"], 9)

    def test_the_settle_ends_only_once_every_model_is_fully_billed(self):
        expected = {"nova-3": 50, "whisper-tiny-en": 50}
        arriving = totals(**{"nova-3": 50, "whisper-tiny-en": 3})
        complete = totals(**{"nova-3": 50, "whisper-tiny-en": 50})
        result = settle([arriving, arriving, complete, complete], expected)
        self.assertTrue(result.complete)
        self.assertEqual(result.polls, 4)
        self.assertEqual(result.missing, [])

    def test_one_matching_read_is_not_enough_on_its_own(self):
        """A count that matches once while data is still moving is not the end."""
        expected = {"nova-3": 50}
        matched = totals(**{"nova-3": 50})
        moved_on = totals(**{"nova-3": 59})
        result = settle([matched, moved_on, moved_on], expected, timeout_s=120)
        self.assertFalse(result.complete)
        self.assertGreaterEqual(cfg.ANALYTICS_SETTLE_COMPLETE_READS, 2)

    def test_the_reported_settle_is_the_lag_that_was_observed(self):
        """Not the lag minus a poll interval, which read as half a second."""
        complete = totals(**{"nova-3": 50})
        result = settle([complete, complete], {"nova-3": 50})
        self.assertTrue(result.complete)
        self.assertGreaterEqual(result.lag_seconds, 0.0)
        self.assertGreaterEqual(result.confirmed_seconds, result.lag_seconds)

    def test_a_dry_run_is_held_to_the_same_invariant(self):
        matching = totals(**{"nova-3": 50})
        self.assertTrue(probe.settled_window(None, None, None, ["nova-3"], {"nova-3": 50},
                                             fake=matching).complete)
        self.assertFalse(probe.settled_window(None, None, None, ["nova-3"], {"nova-3": 50},
                                              fake=totals(**{"nova-3": 3})).complete)


class BoundaryHold(unittest.TestCase):
    """Consecutive windows are held apart by more than the worst observed lag."""

    def test_the_gap_clears_the_worst_lag_the_live_run_saw(self):
        self.assertGreater(quiet.boundary_hold_seconds(), OBSERVED_WORST_LAG_S)

    def test_the_gap_is_never_a_single_minute(self):
        """The live windows were opened a minute apart, which is what shared the tail."""
        self.assertGreater(quiet.boundary_hold_seconds(), 60.0)

    def test_the_gap_is_derived_from_the_settles_measured_so_far(self):
        self.assertAlmostEqual(quiet.boundary_hold_seconds([600.0, 200.0]),
                               quiet.BOUNDARY_HOLD_SETTLE_MULTIPLE * 600.0)

    def test_a_short_measured_settle_never_pulls_the_gap_under_the_floor(self):
        self.assertEqual(quiet.boundary_hold_seconds([0.5, 0.6]),
                         quiet.BOUNDARY_HOLD_FLOOR_S)

    def test_the_next_window_cannot_open_inside_the_last_one_s_lag(self):
        held = []
        end = datetime.now(timezone.utc) - timedelta(seconds=40)
        hold = quiet.boundary_hold_seconds()
        with mock.patch.object(probe.quiet, "sleep_with_progress",
                               side_effect=lambda seconds, label: held.append(seconds)), \
                mock.patch("builtins.print"):
            probe.hold_boundary_gap(end, hold, dry_run=False)
        gap = 40 + held[0]
        self.assertGreater(gap, OBSERVED_WORST_LAG_S)
        self.assertAlmostEqual(gap, hold, places=0)

    def test_a_window_whose_settle_already_ran_past_the_gap_waits_no_longer(self):
        end = datetime.now(timezone.utc) - timedelta(seconds=1800)
        with mock.patch.object(probe.quiet, "sleep_with_progress") as slept, \
                mock.patch("builtins.print"):
            probe.hold_boundary_gap(end, quiet.boundary_hold_seconds(), dry_run=False)
        slept.assert_not_called()

    def test_the_quiet_estimate_counts_the_gap_it_will_hold(self):
        schedule = quiet.QuietSchedule([quiet.Window("w", 10, 100.0)])
        low, _ = schedule.window_range(0)
        self.assertGreaterEqual(low - schedule.windows[0].send_seconds,
                                quiet.boundary_hold_seconds())


# The live P1 run of 2026-08-12, as it was written to billing.windows.jsonl. Every
# one of these disagrees with what its window sent.
LIVE_WINDOWS = {
    "replicate1|1x": [("nova-3", 50, 46), ("whisper-tiny-en", 50, 43)],
    "replicate1|3x": [("nova-3", 50, 59), ("whisper", 50, 37), ("whisper-tiny-en", 50, 3)],
    "replicate2|1x": [("nova-3", 50, 50), ("whisper-turbo", 50, 50),
                      ("whisper-tiny-en", 50, 51)],
}


def live_record(key, rows, settle_seconds=0.6):
    return {
        "probe": "billing",
        "synthetic": False,
        "window_key": key,
        "window_shape": window_log.billing_shape(50, ["nova-3", "whisper"]),
        "measured_at": "2026-08-12T13:50:00Z",
        "settle_seconds_observed": settle_seconds,
        "models": [{"model": model, "requests_sent": sent, "requests_billed": billed,
                    "audio_seconds_ratio": 1.57}
                   for model, sent, billed in rows],
    }


class RecoveringTheCorruptWindows(unittest.TestCase):
    """The four windows already on disk are refused, named, and re-measurable."""

    def log(self):
        return window_log.WindowLog(
            [live_record(key, rows) for key, rows in LIVE_WINDOWS.items()])

    def test_none_of_them_counts_as_a_measurement(self):
        self.assertEqual(self.log().measured, {})
        self.assertEqual(set(self.log().corrupt), set(LIVE_WINDOWS))

    def test_the_window_that_waited_is_refused_over_a_single_extra_request(self):
        """51 billed for 50 sent is one request of somebody else's traffic."""
        log = window_log.WindowLog([live_record("replicate2|1x", LIVE_WINDOWS["replicate2|1x"])])
        self.assertEqual(log.measured, {})
        self.assertEqual(log.mismatches("replicate2|1x"),
                         [{"model": "whisper-tiny-en", "requests_sent": 50,
                           "requests_billed": 51, "delta": 1}])

    def test_a_window_written_before_this_check_existed_is_classified_the_same_way(self):
        """The records on disk carry no `settled` flag; the counts in them are enough."""
        record = live_record("replicate1|3x", LIVE_WINDOWS["replicate1|3x"])
        self.assertNotIn("settled", record)
        self.assertTrue(window_log.count_mismatches(record))

    def test_a_window_recorded_as_failed_is_refused_even_with_no_rows(self):
        record = live_record("replicate1|1x", [])
        record["settled"] = False
        self.assertEqual(window_log.WindowLog([record]).measured, {})

    def test_the_operator_is_told_which_windows_and_how_to_re_measure_them(self):
        planned = [{"key": key, "label": key.replace("|", " at ")} for key in LIVE_WINDOWS]
        lines = "\n".join(window_log.recovery_lines(
            planned, self.log().corrupt, Path("billing.windows.jsonl"),
            "probe_billing.py --live"))
        self.assertIn("3 windows in billing.windows.jsonl", lines)
        for key in LIVE_WINDOWS:
            self.assertIn(key.replace("|", " at "), lines)
        self.assertIn("nova-3 sent 50 billed 59 (+9)", lines)
        self.assertIn("whisper-tiny-en sent 50 billed 3 (-47)", lines)
        self.assertIn("probe_billing.py --live", lines)

    def test_a_clean_log_needs_no_recovery_instructions(self):
        self.assertEqual(window_log.recovery_lines([], {}, Path("x.jsonl"), "cmd"), [])

    def test_re_measuring_supersedes_the_corrupt_record(self):
        good = live_record("replicate1|3x", [("nova-3", 50, 50)], settle_seconds=240.0)
        log = window_log.WindowLog(
            [live_record("replicate1|3x", LIVE_WINDOWS["replicate1|3x"]), good])
        self.assertEqual(set(log.measured), {"replicate1|3x"})
        self.assertEqual(log.corrupt, {})

    def test_a_corrupt_window_is_still_to_run_in_the_plan(self):
        keys = list(LIVE_WINDOWS)
        with mock.patch.object(window_log, "load_windows", return_value=self.log()):
            indices, settles = run_all_completed(keys)
        self.assertEqual(indices, set())
        self.assertEqual(settles, [])


def run_all_completed(keys):
    """run_all's view of which windows are already measured, over a patched log."""
    import run_all
    return run_all.completed_windows(cfg.BILLING_PROBE_RESULT, False, None, keys)


if __name__ == "__main__":
    unittest.main()
