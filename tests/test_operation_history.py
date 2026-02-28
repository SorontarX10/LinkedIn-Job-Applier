from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.operation_history import OperationHistoryTracker


class _CustomValue:
    def __str__(self) -> str:
        return "custom-value"


class OperationHistoryTrackerTest(unittest.TestCase):
    def test_writes_jsonl_run_and_latest_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            tracker = OperationHistoryTracker(base_dir=base_dir, mode="saved_only")

            tracker.log(
                "run_started",
                max_jobs=3,
                queue_sources=["saved", "discovery"],
                path=base_dir / "output",
            )
            tracker.log("job_processing_started", job_url="https://www.linkedin.com/jobs/view/1")
            tracker.finalize(result="ok")

            self.assertTrue(tracker.run_path.exists())
            self.assertTrue(tracker.latest_path.exists())

            run_lines = [
                json.loads(line)
                for line in tracker.run_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            latest_lines = [
                json.loads(line)
                for line in tracker.latest_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertGreaterEqual(len(run_lines), 3)
            self.assertEqual(run_lines, latest_lines)
            self.assertEqual(run_lines[0]["event"], "run_started")
            self.assertEqual(run_lines[0]["mode"], "saved_only")
            self.assertEqual(run_lines[1]["event"], "job_processing_started")
            self.assertEqual(run_lines[-1]["event"], "run_finished")
            self.assertEqual(run_lines[-1]["summary"]["result"], "ok")

            seq_values = [int(item["seq"]) for item in run_lines]
            self.assertEqual(seq_values, list(range(1, len(run_lines) + 1)))
            recent = tracker.recent(limit=2)
            self.assertEqual(len(recent), 2)
            self.assertEqual(recent[-1]["event"], "run_finished")

    def test_sanitizes_nested_and_custom_payload_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tracker = OperationHistoryTracker(base_dir=Path(tmp), mode="discovery_and_apply")
            tracker.log(
                "payload_test",
                nested={"a": 1, "custom": _CustomValue()},
                items=[1, 2, {"ok": True}],
            )

            lines = [
                json.loads(line)
                for line in tracker.run_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(lines), 1)
            payload = lines[0]
            self.assertEqual(payload["event"], "payload_test")
            self.assertEqual(payload["nested"]["a"], 1)
            self.assertEqual(payload["nested"]["custom"], "custom-value")
            self.assertEqual(payload["items"][2]["ok"], True)


if __name__ == "__main__":
    unittest.main()
