from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.run_metrics import RunMetricsTracker


class RunMetricsTrackerTest(unittest.TestCase):
    def test_kpi_report_contains_expected_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            tracker = RunMetricsTracker(base_dir=base_dir, mode="discovery_and_apply")
            tracker.set_queue_context(selected_count=2, sources={"saved", "discovery"})
            tracker.record_discovery_summary(
                {
                    "queries": 2,
                    "discovered": 5,
                    "queued": 2,
                    "rejected": 3,
                }
            )

            tracker.start_job(
                job_url="https://www.linkedin.com/jobs/view/1",
                source="saved",
                started_monotonic=10.0,
            )
            tracker.finish_job(
                job_url="https://www.linkedin.com/jobs/view/1",
                status="submitted",
                notes="ok",
                ended_monotonic=20.0,
            )

            tracker.start_job(
                job_url="https://www.linkedin.com/jobs/view/2",
                source="discovery",
                started_monotonic=30.0,
            )
            tracker.finish_job(
                job_url="https://www.linkedin.com/jobs/view/2",
                status="not_submitted",
                notes="validation blocked",
                ended_monotonic=40.0,
            )

            tracker.record_fallback_trigger(stage="external_submit")
            tracker.record_fallback_outcome(
                stage="external_submit",
                result="clicked",
                playbook_source="memory",
                tool_steps_used=6,
            )
            tracker.record_human_handoff(stage="external_submit", result="requested")

            report_path = tracker.finalize_and_save()
            self.assertIsNotNone(report_path)
            self.assertTrue(Path(report_path).exists())

            payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "discovery_and_apply")
            self.assertEqual(payload["queue_selected_count"], 2)
            self.assertEqual(payload["totals"]["processed_jobs"], 2)
            self.assertEqual(payload["totals"]["submitted"], 1)
            self.assertEqual(payload["totals"]["not_submitted"], 1)
            self.assertEqual(payload["totals"]["fallback_trigger_count"], 1)
            self.assertEqual(payload["totals"]["fallback_recovery_success_count"], 1)
            self.assertEqual(payload["totals"]["human_handoff_count"], 1)
            self.assertEqual(payload["totals"]["playbook_hit_count"], 1)
            self.assertEqual(payload["totals"]["discovery_queued_count"], 2)
            self.assertEqual(payload["totals"]["discovery_submitted_count"], 0)

            kpi = payload["kpi"]
            self.assertAlmostEqual(kpi["application_success_rate"], 0.5, places=4)
            self.assertAlmostEqual(kpi["fallback_trigger_rate"], 0.5, places=4)
            self.assertAlmostEqual(kpi["fallback_recovery_success_rate"], 1.0, places=4)
            self.assertAlmostEqual(kpi["human_handoff_rate"], 0.5, places=4)
            self.assertAlmostEqual(kpi["mean_steps_per_application"], 3.0, places=3)
            self.assertAlmostEqual(kpi["mean_time_per_application_sec"], 10.0, places=3)
            self.assertAlmostEqual(kpi["playbook_hit_rate"], 1.0, places=4)
            self.assertAlmostEqual(kpi["discovery_to_apply_conversion"], 0.0, places=4)

            latest_path = base_dir / "output" / "metrics" / "latest.json"
            history_path = base_dir / "output" / "metrics" / "runs.jsonl"
            self.assertTrue(latest_path.exists())
            self.assertTrue(history_path.exists())


if __name__ == "__main__":
    unittest.main()
