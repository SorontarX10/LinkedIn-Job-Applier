from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.mcp_bridge.spool import McpSpoolStore
from src.models import McpEventEnvelope


class McpSpoolStoreTest(unittest.TestCase):
    def test_enqueue_ack_and_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool_path = Path(tmp) / "mcp_spool.jsonl"
            store = McpSpoolStore(path=spool_path, retry_limit=2, retry_backoff_sec=1)

            event = McpEventEnvelope(
                event_id="event-1",
                run_id="run-1",
                event_type="job_processing_error",
                ts_utc="2026-03-01T00:00:00+00:00",
                payload={"job_url": "https://www.linkedin.com/jobs/view/1"},
            )
            store.enqueue(event, pending_targets=["linear", "notion"])
            due = store.pending_items_due(limit=10)
            self.assertEqual(len(due), 1)

            store.ack("event-1")
            self.assertEqual(store.backlog_count(), 0)

            event2 = McpEventEnvelope(
                event_id="event-2",
                run_id="run-2",
                event_type="job_processing_error",
                ts_utc="2026-03-01T00:00:00+00:00",
                payload={"job_url": "https://www.linkedin.com/jobs/view/2"},
                attempt=2,
            )
            store.enqueue(event2, pending_targets=["linear"])
            due2 = store.pending_items_due(limit=10)
            self.assertEqual(len(due2), 1)
            store.fail(due2[0], pending_targets=["linear"], error="boom")
            self.assertEqual(store.dead_letter_count(), 1)


if __name__ == "__main__":
    unittest.main()

