"""Tests for the settle rule, the excess it records, the boundary hold, and the
recovery from corrupt windows.

These cover the defect a live P1 run exposed on 2026-08-12: windows were
checkpointed as measurements while the analytics were still arriving, and one of
them would have been published as nova-3 billing 1.57x what was sent at 3x.

They also hold the line the same run drew on the other side. Cloudflare bills more
inferences than the client sends, so a settle waiting for the two counts to be
equal never returns; the excess is measured and recorded instead, and it has to
match across speeds before P1's ratio means anything.

Run them with the benchmark's own interpreter, from this directory:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_probe_settle -v
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config as cfg
import excess
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


class ArrivalSettle(unittest.TestCase):
    """The settle is on the whole bill having arrived, not on the reads holding still.

    Cloudflare bills more inferences than the client sends, so the sent count is
    the floor a settled window clears rather than the number it has to equal.
    """

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

    def test_a_window_settles_on_an_excess_that_then_holds(self):
        """52 billed for 50 sent is the platform, and the window is a measurement."""
        over = totals(**{"nova-3": 52})
        result = settle([over, over, over], {"nova-3": 50})
        self.assertTrue(result.complete)
        self.assertTrue(result.usable)
        self.assertEqual(result.missing, [])
        self.assertEqual(result.excess,
                         [{"model": "nova-3", "requests_sent": 50,
                           "requests_billed": 52, "delta": 2}])
        self.assertAlmostEqual(result.excess_rate, 0.04)

    def test_an_excess_still_arriving_is_not_settled_yet(self):
        """Over the floor but still climbing, so the read would catch half the excess."""
        expected = {"nova-3": 50}
        climbing = [totals(**{"nova-3": 51}), totals(**{"nova-3": 52}),
                    totals(**{"nova-3": 53})]
        self.assertFalse(settle(climbing, expected, timeout_s=180).complete)
        landed = climbing + [totals(**{"nova-3": 53})]
        self.assertTrue(settle(landed, expected).complete)

    def test_the_settle_ends_only_once_every_model_has_its_whole_bill(self):
        expected = {"nova-3": 50, "whisper-tiny-en": 50}
        arriving = totals(**{"nova-3": 52, "whisper-tiny-en": 3})
        arrived = totals(**{"nova-3": 52, "whisper-tiny-en": 51})
        result = settle([arriving, arriving, arrived, arrived, arrived], expected)
        self.assertTrue(result.complete)
        self.assertEqual(result.polls, 5)
        self.assertEqual(result.missing, [])

    def test_an_implausible_excess_is_flagged_for_re_measurement(self):
        """150 billed for 50 sent is foreign traffic, not a platform excess."""
        foreign = totals(**{"nova-3": 150})
        result = settle([foreign, foreign, foreign], {"nova-3": 50})
        self.assertTrue(result.complete)
        self.assertFalse(result.usable)
        self.assertEqual([row["delta"] for row in result.implausible], [100])
        self.assertGreater(result.excess_rate, cfg.EXCESS_IMPLAUSIBLE_ABOVE)

    def test_the_worst_excess_the_platform_produced_is_still_a_measurement(self):
        """59 billed for 50 sent under four-model load, which is the platform at 18%."""
        observed = totals(**{"nova-3": 59})
        result = settle([observed, observed, observed], {"nova-3": 50})
        self.assertTrue(result.usable)
        self.assertEqual(result.implausible, [])

    def test_the_reported_settle_is_the_lag_that_was_observed(self):
        """Not the lag minus a poll interval, which read as half a second."""
        arrived = totals(**{"nova-3": 51})
        result = settle([arrived, arrived, arrived], {"nova-3": 50})
        self.assertTrue(result.complete)
        self.assertGreaterEqual(result.lag_seconds, 0.0)
        self.assertGreaterEqual(result.confirmed_seconds, result.lag_seconds)

    def test_a_dry_run_is_held_to_the_same_invariant(self):
        at_the_floor = totals(**{"nova-3": 50})
        self.assertTrue(probe.settled_window(None, None, None, ["nova-3"], {"nova-3": 50},
                                             fake=at_the_floor).usable)
        self.assertFalse(probe.settled_window(None, None, None, ["nova-3"], {"nova-3": 50},
                                              fake=totals(**{"nova-3": 3})).complete)
        self.assertFalse(probe.settled_window(None, None, None, ["nova-3"], {"nova-3": 50},
                                              fake=totals(**{"nova-3": 150})).usable)


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


# The live P1 run of 2026-08-12, as it was written to billing.windows.jsonl. The
# first two hold models billed for less than they sent, which is a bill that never
# finished arriving. The third is over by one request, which is the platform.
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


CORRUPT_LIVE_WINDOWS = ("replicate1|1x", "replicate1|3x")


class RecoveringTheCorruptWindows(unittest.TestCase):
    """The windows already on disk whose bill never arrived are refused, named, and re-measurable."""

    def log(self):
        return window_log.WindowLog(
            [live_record(key, rows) for key, rows in LIVE_WINDOWS.items()])

    def test_a_window_billed_for_less_than_it_sent_is_not_a_measurement(self):
        self.assertEqual(set(self.log().corrupt), set(CORRUPT_LIVE_WINDOWS))

    def test_the_window_that_waited_is_a_measurement_and_carries_its_excess(self):
        """51 billed for 50 sent is the platform's own excess, which is the result."""
        log = window_log.WindowLog([live_record("replicate2|1x", LIVE_WINDOWS["replicate2|1x"])])
        self.assertEqual(set(log.measured), {"replicate2|1x"})
        self.assertEqual(log.corrupt, {})
        rows = excess.record_rows(log.measured["replicate2|1x"])
        self.assertEqual([row["delta"] for row in rows], [0, 0, 1])

    def test_a_window_written_before_this_check_existed_is_classified_the_same_way(self):
        """The records on disk carry no `settled` flag; the counts in them are enough."""
        record = live_record("replicate1|3x", LIVE_WINDOWS["replicate1|3x"])
        self.assertNotIn("settled", record)
        self.assertIn("billed for less than it sent", window_log.refusal(record))

    def test_a_window_recorded_as_failed_is_refused_even_with_no_rows(self):
        record = live_record("replicate1|1x", [])
        record["settled"] = False
        self.assertEqual(window_log.WindowLog([record]).measured, {})

    def test_a_window_with_more_excess_than_the_platform_produces_is_refused(self):
        """Three times what was sent is foreign traffic inside the window."""
        record = live_record("replicate1|3x", [("nova-3", 50, 150)])
        self.assertEqual(window_log.WindowLog([record]).measured, {})
        self.assertIn("foreign traffic", window_log.refusal(record))
        self.assertEqual([row["delta"] for row in window_log.implausible_excess(record)], [100])

    def test_the_operator_is_told_which_windows_and_how_to_re_measure_them(self):
        planned = [{"key": key, "label": key.replace("|", " at ")} for key in LIVE_WINDOWS]
        lines = "\n".join(window_log.recovery_lines(
            planned, self.log().corrupt, Path("billing.windows.jsonl"),
            "probe_billing.py --live"))
        self.assertIn("2 windows in billing.windows.jsonl", lines)
        for key in CORRUPT_LIVE_WINDOWS:
            self.assertIn(key.replace("|", " at "), lines)
        self.assertIn("whisper sent 50 billed 37 (-13)", lines)
        self.assertIn("whisper-tiny-en sent 50 billed 3 (-47)", lines)
        self.assertIn("probe_billing.py --live", lines)

    def test_a_clean_log_needs_no_recovery_instructions(self):
        self.assertEqual(window_log.recovery_lines([], {}, Path("x.jsonl"), "cmd"), [])

    def test_re_measuring_supersedes_the_corrupt_record(self):
        good = live_record("replicate1|3x", [("nova-3", 50, 52)], settle_seconds=240.0)
        log = window_log.WindowLog(
            [live_record("replicate1|3x", LIVE_WINDOWS["replicate1|3x"]), good])
        self.assertEqual(set(log.measured), {"replicate1|3x"})
        self.assertEqual(log.corrupt, {})

    def test_a_corrupt_window_is_still_to_run_in_the_plan(self):
        keys = list(LIVE_WINDOWS)
        with mock.patch.object(window_log, "load_windows", return_value=self.log()):
            indices, settles = run_all_completed(keys)
        self.assertEqual(indices, {keys.index("replicate2|1x")})
        self.assertEqual(settles, [0.6])


class ExcessAcrossSpeeds(unittest.TestCase):
    """P1's ratio may only be published when the excess rate matches across speeds.

    A billed request the client never issued carries billed seconds with it, so an
    excess rate that differs between 1x and 3x moves the very ratio the probe
    reports.
    """

    def windows(self, base_billed, fast_billed):
        return [
            {"speed": 1.0, "models": [{"model": "nova-3", "requests_sent": 50,
                                       "requests_billed": base_billed}]},
            {"speed": 3.0, "models": [{"model": "nova-3", "requests_sent": 50,
                                       "requests_billed": fast_billed}]},
        ]

    def test_rates_that_agree_leave_the_ratio_standing(self):
        result = excess.compare_speeds(self.windows(52, 52), 1.0)
        self.assertTrue(result["comparable"])
        self.assertAlmostEqual(result["per_speed"]["1"]["excess_rate"], 0.04)
        self.assertAlmostEqual(result["spread_against_baseline"]["3"], 0.0)
        self.assertIn("not biased by the excess", result["statement"])

    def test_rates_that_diverge_make_the_ratio_untrustworthy(self):
        result = excess.compare_speeds(self.windows(52, 59), 1.0)
        self.assertFalse(result["comparable"])
        self.assertAlmostEqual(result["spread_against_baseline"]["3"], 0.14)
        self.assertIn("no ratio is published", result["statement"])

    def test_the_budget_is_the_tolerance_the_ratio_itself_is_held_to(self):
        self.assertEqual(cfg.EXCESS_SPREAD_BUDGET, probe.TOLERANCE)
        inside = excess.compare_speeds(self.windows(50, 51), 1.0)
        self.assertTrue(inside["comparable"])
        outside = excess.compare_speeds(self.windows(50, 52), 1.0)
        self.assertFalse(outside["comparable"])

    def test_a_speed_with_no_baseline_to_compare_against_is_not_trustworthy(self):
        only_fast = [self.windows(52, 52)[1]]
        result = excess.compare_speeds(only_fast, 1.0)
        self.assertFalse(result["comparable"])
        self.assertIn("not trustworthy", result["statement"])

    def test_the_per_speed_rates_are_reported_side_by_side(self):
        result = excess.compare_speeds(self.windows(52, 59), 1.0)
        self.assertIn("1x +4.0%", result["statement"])
        self.assertIn("3x +18.0%", result["statement"])


def run_all_completed(keys):
    """run_all's view of which windows are already measured, over a patched log."""
    import run_all
    return run_all.completed_windows(cfg.BILLING_PROBE_RESULT, False, None, keys)


if __name__ == "__main__":
    unittest.main()
