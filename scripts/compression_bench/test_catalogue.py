"""Tests for the worker's model catalogue and the cost arithmetic built on it.

No test opens a socket: the fetch is stubbed and the cache is a temporary file.

Run them with the benchmark's own interpreter, from this directory:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_catalogue -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import catalogue
import config as cfg

PUBLISHED = {
    "models": [
        {"key": "nova-3", "label": "Deepgram Nova-3", "model": "@cf/deepgram/nova-3",
         "usdPerAudioMinute": 0.0052, "freeAudioMinutesPerDay": 21},
        {"key": "whisper-tiny-en", "label": "Whisper tiny (English)",
         "model": "@cf/openai/whisper-tiny-en",
         "usdPerAudioMinute": 0.0000066, "freeAudioMinutesPerDay": 16556},
    ],
}


def catalogue_of(body=PUBLISHED):
    return catalogue.Catalogue(catalogue.parse(body), "2026-08-12T09:00:00Z", "https://worker")


class Parse(unittest.TestCase):
    def test_entries_are_keyed_by_the_worker_key(self):
        models = catalogue.parse(PUBLISHED)
        self.assertEqual(sorted(models), ["nova-3", "whisper-tiny-en"])

    def test_a_body_with_no_models_is_refused(self):
        with self.assertRaises(ValueError):
            catalogue.parse({"models": []})

    def test_an_entry_missing_a_billing_figure_is_refused(self):
        body = {"models": [{"key": "nova-3", "model": "@cf/deepgram/nova-3",
                            "usdPerAudioMinute": 0.0052}]}
        with self.assertRaises(ValueError) as caught:
            catalogue.parse(body)
        self.assertIn("freeAudioMinutesPerDay", str(caught.exception))


class Lookups(unittest.TestCase):
    def test_the_analytics_id_comes_from_the_catalogue(self):
        self.assertEqual(catalogue_of().model_id("nova-3"), "@cf/deepgram/nova-3")

    def test_an_analytics_id_maps_back_to_the_worker_key(self):
        self.assertEqual(catalogue_of().key_for_model_id("@cf/deepgram/nova-3"), "nova-3")

    def test_an_unknown_analytics_id_is_kept_verbatim(self):
        self.assertEqual(catalogue_of().key_for_model_id("@cf/other/model"), "@cf/other/model")

    def test_a_model_the_worker_does_not_publish_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            catalogue_of().usd_per_audio_minute("whisper")
        self.assertIn("whisper", str(caught.exception))


class Load(unittest.TestCase):
    def test_a_reachable_worker_is_used_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models-catalogue.json"
            with mock.patch.object(catalogue, "fetch",
                                   return_value=catalogue.parse(PUBLISHED)):
                loaded = catalogue.load("https://worker", "token", cache)
            self.assertFalse(loaded.from_cache)
            self.assertIn("fetched from https://worker/models", loaded.provenance())
            self.assertEqual(sorted(json.loads(cache.read_text())["models"][0]),
                             sorted(PUBLISHED["models"][0]))

    def test_an_unreachable_worker_falls_back_to_the_cache_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models-catalogue.json"
            catalogue.write_cache(cache, catalogue.parse(PUBLISHED), "https://worker",
                                  "2026-08-01T09:00:00Z")
            with mock.patch.object(catalogue, "fetch", side_effect=OSError("connection refused")):
                loaded = catalogue.load("https://worker", "token", cache)
            self.assertTrue(loaded.from_cache)
            self.assertEqual(loaded.fetched_at, "2026-08-01T09:00:00Z")
            self.assertIn("STALE", loaded.provenance())
            self.assertIn("connection refused", loaded.provenance())
            self.assertIn("2026-08-01T09:00:00Z", loaded.provenance())

    def test_no_worker_and_no_cache_names_what_to_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models-catalogue.json"
            with self.assertRaises(SystemExit) as caught:
                catalogue.load("", "", cache)
            message = str(caught.exception)
            self.assertIn("CLOUD_DICTATION_WORKER", message)
            self.assertIn("CLOUD_DICTATION_TOKEN", message)
            self.assertIn(str(cache), message)

    def test_an_unreachable_worker_with_no_cache_is_never_a_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models-catalogue.json"
            with mock.patch.object(catalogue, "fetch", side_effect=OSError("connection refused")):
                with self.assertRaises(SystemExit) as caught:
                    catalogue.load("https://worker", "token", cache)
            self.assertIn("connection refused", str(caught.exception))
            self.assertIn("no built-in rates", str(caught.exception).lower())


class CostFromTheCatalogue(unittest.TestCase):
    """The published figures are the only input to the cost arithmetic."""

    def setUp(self):
        self._saved = cfg._catalogue
        cfg._catalogue = catalogue_of()
        self.addCleanup(setattr, cfg, "_catalogue", self._saved)

    def test_an_hour_costs_sixty_published_minutes(self):
        self.assertAlmostEqual(cfg.usd_per_hour("nova-3", 1.0), 0.0052 * 60)

    def test_compression_divides_the_cost_by_r(self):
        self.assertAlmostEqual(cfg.usd_per_hour("nova-3", 3.0), 0.0052 * 60 / 3)

    def test_compression_multiplies_the_free_minutes_by_r(self):
        self.assertAlmostEqual(cfg.free_minutes_per_day("nova-3", 1.0), 21)
        self.assertAlmostEqual(cfg.free_minutes_per_day("nova-3", 2.5), 21 * 2.5)

    def test_the_cheapest_model_reads_from_the_catalogue_too(self):
        self.assertAlmostEqual(cfg.usd_per_hour("whisper-tiny-en", 1.0), 0.0000066 * 60)
        self.assertAlmostEqual(cfg.free_minutes_per_day("whisper-tiny-en", 1.0), 16556)


if __name__ == "__main__":
    unittest.main()
