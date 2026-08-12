"""The report's two halves: what it may state, and what it must refuse to state.

The cost half is arithmetic on the worker's published rates, which have been
checked against real billing, so it renders with no results file at all. The
accuracy half must render an empty state instead of a number whenever the run
behind it has not happened, and these tests hold that line: no chart, no table
and no figure number appears for a section with nothing behind it.

No test opens a socket. The catalogue is injected, so the rates under test are
the ones the worker publishes and never a copy kept here.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import catalogue
import config as cfg
import excess
import report

PUBLISHED = {
    "models": [
        {"key": "nova-3", "label": "Deepgram Nova-3", "model": "@cf/deepgram/nova-3",
         "usdPerAudioMinute": 0.0052, "freeAudioMinutesPerDay": 21},
        {"key": "whisper-turbo", "label": "Whisper large-v3-turbo",
         "model": "@cf/openai/whisper-large-v3-turbo",
         "usdPerAudioMinute": 0.000513, "freeAudioMinutesPerDay": 214},
        {"key": "whisper", "label": "Whisper (base)", "model": "@cf/openai/whisper",
         "usdPerAudioMinute": 0.000453, "freeAudioMinutesPerDay": 243},
        {"key": "whisper-tiny-en", "label": "Whisper tiny (English)",
         "model": "@cf/openai/whisper-tiny-en",
         "usdPerAudioMinute": 0.0000066, "freeAudioMinutesPerDay": 16556},
    ],
}


class ReportTestCase(unittest.TestCase):
    """A report rendered into a temporary run directory, off an injected catalogue."""

    from_cache = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patched = {
            "RUN_DIR": root,
            "RESULTS": root / "results.json",
            "REPORT": root / "report.html",
            "DRY_RUN_RESULTS": root / "results.dry-run.json",
            "DRY_RUN_REPORT": root / "report.dry-run.html",
            "BILLING_PROBE_RESULT": root / "billing.json",
            "SILENCE_PROBE_RESULT": root / "silence.json",
        }
        self.saved = {name: getattr(cfg, name) for name in self.patched}
        for name, value in self.patched.items():
            setattr(cfg, name, value)
        saved_catalogue = cfg._catalogue
        cfg._catalogue = catalogue.Catalogue(
            catalogue.parse(PUBLISHED), "2026-08-12T09:00:00Z", "https://worker",
            from_cache=self.from_cache,
            cache_path=root / "models-catalogue.json",
            unreachable="OSError: connection refused" if self.from_cache else None)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, cfg, "_catalogue", saved_catalogue)
        self.addCleanup(lambda: [setattr(cfg, k, v) for k, v in self.saved.items()])

    def render(self, *argv):
        saved_argv = sys.argv
        sys.argv = ["report.py", *argv]
        try:
            report.main()
        finally:
            sys.argv = saved_argv
        path = self.patched["DRY_RUN_REPORT"] if "--dry-run" in argv else self.patched["REPORT"]
        return path.read_text()


class CostHalfTest(ReportTestCase):
    def test_renders_with_no_results_file(self):
        html = self.render("--live")
        self.assertIn("Free-tier reach", html)
        self.assertIn("The rates were checked against a bill", html)

    def test_states_the_rate_check_as_a_finding(self):
        html = self.render("--live")
        self.assertIn("All four agree to within 0.5 percent", html)
        self.assertIn(report.RATE_CHECK_DATE, html)
        for model_key in cfg.MODELS:
            self.assertIn(f"{report.RATE_CHECK_GAP[model_key] * 100:.2f}%", html)

    def test_the_rate_check_stores_a_measurement_and_never_a_rate(self):
        # A gap is a share, so nothing here can be mistaken for a price to cost
        # with if the worker's catalogue ever goes away.
        for model_key, gap in report.RATE_CHECK_GAP.items():
            self.assertIn(model_key, cfg.MODELS)
            self.assertLess(gap, 0.01, f"{model_key} no longer agrees with billing")

    def test_every_cost_figure_is_the_catalogue_arithmetic(self):
        html = self.render("--live")
        for model_key in cfg.MODELS:
            self.assertIn(report.per_minute(cfg.usd_per_audio_minute(model_key)), html)
            for speed in cfg.SPEEDS:
                self.assertIn(f"${cfg.usd_per_hour(model_key, speed):.5f}", html)

    def test_the_prices_come_from_the_catalogue_and_not_from_the_harness(self):
        html = self.render("--live")
        # Nova-3 at 1x is the catalogue's own 0.0052 per audio minute times 60.
        self.assertIn("$0.31200", html)
        cfg._catalogue.models["nova-3"]["usdPerAudioMinute"] = 0.0104
        self.assertIn("$0.62400", self.render("--live"))

    def test_free_tier_chart_omits_the_model_that_is_off_the_scale(self):
        html = self.render("--live")
        section = html.split("Free-tier reach")[1].split("Cost per hour")[0]
        chart = section[section.index("<svg"):section.index("</svg>")]
        for model_key in report.FREE_TIER_CHART_OMITS:
            self.assertNotIn(cfg.MODELS[model_key]["label"], chart)
        # The omitted model still has tiles and a table row around the chart.
        self.assertIn(cfg.MODELS["whisper-tiny-en"]["label"], section)


class AccuracyHalfTest(ReportTestCase):
    def test_every_accuracy_section_is_an_empty_state_without_results(self):
        html = self.render("--live")
        # Seven accuracy sections, the two probes, and the per-speed excess
        # comparison, which waits on the same probe run the two probes do.
        self.assertEqual(html.count('class="pending"'), 10)
        self.assertIn("No data yet", html)

    def test_every_label_is_numbered_in_the_order_it_is_read(self):
        html = self.render("--live")
        # Every label site, including the one inside a details block, which does
        # not carry the caption class the others do.
        for kind in ("Figure", "Table"):
            found = re.findall(rf'\b{kind} (\d+)\b', html)
            self.assertEqual(found, [str(i) for i in range(1, len(found) + 1)])
            self.assertTrue(found)

    def test_no_accuracy_figure_or_table_is_numbered_without_results(self):
        html = self.render("--live")
        # Two cost figures and four measured tables, and nothing else claims a
        # label. The fourth table is the billing excess, which is measured.
        self.assertIn("Figure 2", html)
        self.assertNotIn("Figure 3", html)
        self.assertIn("Table 4", html)
        self.assertNotIn("Table 5", html)

    def test_no_accuracy_wording_can_be_read_as_a_result(self):
        html = self.render("--live")
        for claim in ("WER</th>", "ΔWER, percentage points", "% WER"):
            self.assertNotIn(claim, html)

    def test_a_missing_named_results_file_is_still_an_error(self):
        with self.assertRaises(SystemExit):
            self.render("--live", "--results", "nothing-here.json")


def billing_result(base_billed, fast_billed, comparable):
    """A P1 result file carrying one 1x window and one 3x window."""
    def window(speed, billed):
        return {
            "speed": speed,
            "settle_seconds_observed": 200.0,
            "models": [{"model": "nova-3", "requests_sent": 50, "requests_billed": billed,
                        "audio_seconds_billed": 600.0 / speed, "audio_seconds_sent": 600.0,
                        "audio_seconds_ratio": 1.0, "neurons_billed": 0.0,
                        "neurons_worker": 0.0, "neurons_ratio": None, "billed_as_sent": True}],
        }
    windows = [window(1.0, base_billed), window(3.0, fast_billed)]
    comparison = excess.compare_speeds(windows, 1.0)
    assert comparison["comparable"] is comparable
    return {
        "probe": "P1 billed duration under compression",
        "synthetic": False,
        "speeds": [1.0, 3.0],
        "clips_per_window": 50,
        "audio_minutes_per_replicate": 10.0,
        "request_source_filter": "unknown",
        "tolerance": 0.02,
        "measurement_span": None,
        "billing_excess": comparison,
        "ratio_trustworthy": comparison["comparable"],
        "settle_seconds_observed": {"mean": 200.0, "max": 200.0, "windows": [200.0, 200.0]},
        "replicates": [{"replicate": 1, "windows": windows}],
        "summary": {"nova-3": {"billed_as_sent": True, "ratio_trustworthy": comparison["comparable"],
                               "proportionality": {"3": {"expected_billed_fraction": 1 / 3,
                                                         "observed_billed_fraction": 1 / 3,
                                                         "replicates": [1 / 3],
                                                         "proportional": True}}}},
    }


class BillingExcessTest(ReportTestCase):
    """The excess is stated as measured, its cause as a hypothesis, and the ratio is gated on it."""

    def write_probe(self, data):
        self.patched["BILLING_PROBE_RESULT"].write_text(json.dumps(data))

    def test_the_observed_counts_are_stated_as_measurement(self):
        html = self.render("--live")
        self.assertIn("The platform bills more inferences than the client sends", html)
        for row in report.EXCESS_OBSERVATIONS:
            self.assertIn(f'<td class="n">{row["billed"]}</td>', html)
        self.assertIn("+18%", html)

    def test_the_cause_is_labelled_a_hypothesis_and_the_behaviour_is_not(self):
        html = self.render("--live")
        section = html.split('id="excess"')[1].split('id="corpus"')[0]
        self.assertIn("The billing behaviour above is measured. Why it happens is not.", section)
        self.assertIn("hypothesis", section)

    def test_the_usage_counter_consequence_is_stated(self):
        section = self.render("--live").split('id="excess"')[1]
        self.assertIn("usage counter", section)
        self.assertIn("free daily allowance", section)

    def test_the_per_speed_comparison_is_an_empty_state_until_the_probe_runs(self):
        section = self.render("--live").split('id="excess"')[1].split('id="corpus"')[0]
        self.assertIn('class="pending"', section)
        self.assertNotIn("+4.0%", section)

    def test_rates_that_agree_publish_both_the_comparison_and_the_ratio(self):
        self.write_probe(billing_result(52, 52, comparable=True))
        html = self.render("--live")
        self.assertIn("not biased by the excess", html)
        self.assertIn("Billed at 3x, share of 1x", html)
        self.assertNotIn("No ratio published", html)

    def test_rates_that_diverge_withhold_the_ratio_and_say_why(self):
        self.write_probe(billing_result(52, 59, comparable=False))
        html = self.render("--live")
        self.assertIn("No ratio published", html)
        self.assertIn("no ratio is published from these windows", html)
        self.assertNotIn("Billed at 3x, share of 1x", html)

    def test_a_dry_run_excess_is_never_presented_as_measured(self):
        data = billing_result(50, 50, comparable=True)
        data["synthetic"] = True
        self.write_probe(data)
        html = self.render("--live")
        self.assertIn("zero by construction and measures nothing", html)

    def test_both_speeds_excess_rates_are_shown_side_by_side(self):
        self.write_probe(billing_result(52, 59, comparable=False))
        html = self.render("--live")
        self.assertIn("+4.0%", html)
        self.assertIn("+18.0%", html)


class StaleCatalogueTest(ReportTestCase):
    """Rates served from the cache are never presented as confirmed."""

    from_cache = True

    def test_the_cost_sections_are_marked_stale_rather_than_confirmed(self):
        html = self.render("--live")
        self.assertIn("stale rates", html)
        self.assertNotIn('class="state done">confirmed', html)

    def test_the_stale_banner_and_the_footer_both_say_where_the_rates_came_from(self):
        html = self.render("--live")
        self.assertIn("Stale rates", html)
        self.assertIn("connection refused", html)
        self.assertIn("cached copy", html)


class NumberingTest(unittest.TestCase):
    def test_labels_are_contiguous_in_the_order_the_document_reads(self):
        numbering = report.Numbering()
        document = f"{numbering.figure()} {numbering.table()} {numbering.figure()}"
        self.assertEqual(report.Numbering.resolve(document), "Figure 1 Table 1 Figure 2")

    def test_a_body_built_out_of_order_still_numbers_in_reading_order(self):
        """Bodies are built in a different order from the one they are laid out in."""
        numbering = report.Numbering()
        built_last, built_first = numbering.table(), numbering.table()
        self.assertEqual(report.Numbering.resolve(f"{built_first} {built_last}"),
                         "Table 1 Table 2")


class FreeReachTest(ReportTestCase):
    def test_reads_in_minutes_until_the_allowance_runs_to_hundreds_of_hours(self):
        self.assertEqual(report.free_reach("nova-3", 1.0), "21 min")
        self.assertEqual(report.free_reach("nova-3", 2.0), "42 min")
        self.assertEqual(report.free_reach("whisper", 3.0), "729 min")
        self.assertEqual(report.free_reach("whisper-tiny-en", 1.0), "276 hr")


if __name__ == "__main__":
    unittest.main()
