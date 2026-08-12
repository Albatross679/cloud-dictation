"""The report's two halves: what it may state, and what it must refuse to state.

The cost half is arithmetic on the worker's published rates, which have been
checked against real billing, so it renders with no results file at all. The
accuracy half must render an empty state instead of a number whenever the run
behind it has not happened, and these tests hold that line: no chart, no table
and no figure number appears for a section with nothing behind it.

No test opens a socket. The catalogue is injected, so the rates under test are
the ones the worker publishes and never a copy kept here.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import catalogue
import config as cfg
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
        self.assertEqual(html.count('class="pending"'), 9)
        self.assertIn("No data yet", html)

    def test_no_accuracy_figure_or_table_is_numbered_without_results(self):
        html = self.render("--live")
        # Two cost figures and three cost tables, and nothing else claims a label.
        self.assertIn("Figure 2", html)
        self.assertNotIn("Figure 3", html)
        self.assertIn("Table 3", html)
        self.assertNotIn("Table 4", html)

    def test_no_accuracy_wording_can_be_read_as_a_result(self):
        html = self.render("--live")
        for claim in ("WER</th>", "ΔWER, percentage points", "% WER"):
            self.assertNotIn(claim, html)

    def test_a_missing_named_results_file_is_still_an_error(self):
        with self.assertRaises(SystemExit):
            self.render("--live", "--results", "nothing-here.json")


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
    def test_labels_are_contiguous_in_the_order_they_are_taken(self):
        numbering = report.Numbering()
        self.assertEqual(numbering.figure(), "Figure 1")
        self.assertEqual(numbering.table(), "Table 1")
        self.assertEqual(numbering.figure(), "Figure 2")
        self.assertEqual(numbering.table(), "Table 2")


class FreeReachTest(ReportTestCase):
    def test_reads_in_minutes_until_the_allowance_runs_to_hundreds_of_hours(self):
        self.assertEqual(report.free_reach("nova-3", 1.0), "21 min")
        self.assertEqual(report.free_reach("nova-3", 2.0), "42 min")
        self.assertEqual(report.free_reach("whisper", 3.0), "729 min")
        self.assertEqual(report.free_reach("whisper-tiny-en", 1.0), "276 hr")


if __name__ == "__main__":
    unittest.main()
