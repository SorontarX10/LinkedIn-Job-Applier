from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.job_discovery import DiscoveryQuery, JobDiscovery
from src.job_queue import JobQueueStore


class _FakeMouse:
    def wheel(self, x: int, y: int) -> None:
        del x, y


class _FakeDiscoveryPage:
    def __init__(self, cards):
        self.cards = cards
        self.goto_count = 0
        self.mouse = _FakeMouse()

    def is_closed(self) -> bool:
        return False

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        del url, wait_until
        self.goto_count += 1

    def wait_for_timeout(self, ms: int) -> None:
        del ms

    def evaluate(self, script: str):
        del script
        return self.cards


class DiscoveryCacheAndQueueRetryTest(unittest.TestCase):
    def test_discovery_text_cleanup_deduplicates_noise(self) -> None:
        self.assertEqual(
            JobDiscovery._clean_discovery_title("Head of SEO | RemoteHead of SEO | Remote"),
            "Head of SEO | Remote",
        )
        self.assertEqual(
            JobDiscovery._clean_discovery_title("Expert AI Engineer Expert AI Engineer with verification"),
            "Expert AI Engineer",
        )
        self.assertEqual(
            JobDiscovery._clean_discovery_company("with verification"),
            "",
        )
        self.assertEqual(
            JobDiscovery._clean_discovery_location("Warsaw, Mazowieckie, Poland Be an early applicant 2 days ago"),
            "Warsaw, Mazowieckie, Poland",
        )

    def test_discovery_uses_cache_for_same_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache_path = base / "data" / "job_discovery_cache.json"
            discovery = JobDiscovery(cache_path=cache_path, cache_ttl_minutes=90)
            queries = [DiscoveryQuery(keywords="agentic ai", location="Poland", days_back=30)]

            page_1 = _FakeDiscoveryPage(
                cards=[
                    {
                        "href": "/jobs/view/999/",
                        "title": "Agentic AI Engineer",
                        "company": "Example",
                        "location": "Poland",
                        "snippet": "Easy Apply role",
                    }
                ]
            )
            first = discovery.discover_jobs(page=page_1, queries=queries, max_results=10, pages_per_query=1)
            self.assertEqual(len(first), 1)
            self.assertEqual(page_1.goto_count, 1)
            self.assertTrue(cache_path.exists())

            page_2 = _FakeDiscoveryPage(cards=[])
            second = discovery.discover_jobs(page=page_2, queries=queries, max_results=10, pages_per_query=1)
            self.assertEqual(len(second), 1)
            self.assertEqual(page_2.goto_count, 0)

    def test_queue_retry_limit_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "data" / "job_queue.jsonl"
            store = JobQueueStore(
                queue_path,
                retry_limit=2,
                retry_cooldown_minutes=30,
            )
            url = "https://www.linkedin.com/jobs/view/777/"
            store.enqueue_discovery_jobs(
                [
                    {
                        "job_id": "777",
                        "url": url,
                        "title": "QA role",
                        "company": "Retry Inc",
                        "location": "Poland",
                        "score": 80,
                        "hard_reject": False,
                        "source": "discovery",
                    }
                ]
            )

            normalized_url = "https://www.linkedin.com/jobs/view/777"
            store.mark_in_progress(url)
            store.sync_from_application_record(
                url,
                {
                    "status": "not_submitted",
                    "title": "QA role",
                    "company": "Retry Inc",
                    "notes": "validation blocked",
                },
                source="discovery",
            )

            # Cooldown active after recent attempt.
            queued_now = store.get_top_queued_urls(limit=5, sources={"discovery"})
            self.assertEqual(queued_now, [])

            # Simulate old attempt time to pass cooldown.
            store._items[normalized_url]["last_attempt_at_utc"] = "2000-01-01T00:00:00+00:00"
            queued_after_cooldown = store.get_top_queued_urls(limit=5, sources={"discovery"})
            self.assertEqual(queued_after_cooldown, [normalized_url])

            # Retry limit reached -> hidden from queue.
            store._items[normalized_url]["retry_count"] = 2
            queued_after_limit = store.get_top_queued_urls(limit=5, sources={"discovery"})
            self.assertEqual(queued_after_limit, [])


if __name__ == "__main__":
    unittest.main()
