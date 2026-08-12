"""Tests for the per-mode resume log and its guard.

Run them with the benchmark's own interpreter, from this directory:

    ../../runs/compression-bench/.venv/bin/python -m unittest test_response_log -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import config as cfg
import response_log
import run


def record(utt_id, speed, model, synthetic, ok=True):
    return {"utt_id": utt_id, "speed": speed, "model": model, "synthetic": synthetic, "ok": ok}


def write_log(directory, name, records):
    path = Path(directory) / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


class ModePaths(unittest.TestCase):
    def test_each_mode_owns_its_artifacts(self):
        for path_for in (cfg.responses_path, cfg.results_path, cfg.report_path):
            self.assertNotEqual(path_for(True), path_for(False))

    def test_live_keeps_the_undecorated_names(self):
        self.assertEqual(cfg.responses_path(False), cfg.RESPONSES)
        self.assertEqual(cfg.results_path(False), cfg.RESULTS)
        self.assertEqual(cfg.report_path(False), cfg.REPORT)


class VerifyResponses(unittest.TestCase):
    def test_missing_log_is_an_empty_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.jsonl"
            self.assertEqual(response_log.verify_responses(path, dry_run=False), [])

    def test_matching_mode_returns_the_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "responses.jsonl", [record("a", 1.0, "whisper", False)])
            self.assertEqual(len(response_log.verify_responses(path, dry_run=False)), 1)

    def test_live_refuses_a_synthetic_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "responses.jsonl", [record("a", 1.0, "whisper", True)])
            with self.assertRaises(SystemExit) as caught:
                response_log.verify_responses(path, dry_run=False)
            self.assertIn(str(path), str(caught.exception))
            self.assertIn("dry run", str(caught.exception))

    def test_dry_run_refuses_a_real_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "responses.dry-run.jsonl", [record("a", 1.0, "whisper", False)])
            with self.assertRaises(SystemExit) as caught:
                response_log.verify_responses(path, dry_run=True)
            self.assertIn(str(path), str(caught.exception))

    def test_a_mixed_log_is_refused_by_both_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "responses.jsonl", [
                record("a", 1.0, "whisper", True),
                record("b", 1.0, "whisper", False),
            ])
            for dry_run in (True, False):
                with self.assertRaises(SystemExit) as caught:
                    response_log.verify_responses(path, dry_run=dry_run)
                self.assertIn("mixed", str(caught.exception))
                self.assertIn(str(path), str(caught.exception))


class VerifyResults(unittest.TestCase):
    def test_live_refuses_results_scored_from_synthetic_responses(self):
        with self.assertRaises(SystemExit):
            response_log.verify_results(cfg.RESULTS, False, {"responses": 10, "synthetic": 10})

    def test_dry_run_refuses_results_scored_from_real_responses(self):
        with self.assertRaises(SystemExit):
            response_log.verify_results(cfg.DRY_RUN_RESULTS, True, {"responses": 10, "synthetic": 0})

    def test_matching_results_pass(self):
        response_log.verify_results(cfg.RESULTS, False, {"responses": 10, "synthetic": 0})
        response_log.verify_results(cfg.DRY_RUN_RESULTS, True, {"responses": 10, "synthetic": 10})


class LoadDone(unittest.TestCase):
    def test_resume_counts_only_completed_cells_of_this_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "responses.dry-run.jsonl", [
                record("a", 1.0, "whisper", True),
                record("b", 2.0, "whisper", True, ok=False),
            ])
            self.assertEqual(run.load_done(path, dry_run=True), {"a|1|whisper"})

    def test_a_live_run_never_resumes_over_a_synthetic_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(tmp, "responses.jsonl", [record("a", 1.0, "whisper", True)])
            with self.assertRaises(SystemExit):
                run.load_done(path, dry_run=False)


if __name__ == "__main__":
    unittest.main()
