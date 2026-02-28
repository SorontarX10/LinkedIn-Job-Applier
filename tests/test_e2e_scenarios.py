from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from src.config import Settings
from src.job_discovery import DiscoveryJob, DiscoveryQuery
from src.knowledge_store import KnowledgeStore
from src.linkedin_bot import LinkedInJobApplier
from src.models import DiscoveryScore, FitDecision, JobPosting


class StubLLM:
    def __init__(self, fit_decision: FitDecision | None = None):
        self._fit_decision = fit_decision or FitDecision(
            should_apply=True,
            fit_score=90,
            reasoning="stub",
        )

    def analyze_job(
        self,
        job: JobPosting,
        cv_text: str,
        profile: dict[str, str],
        known_answers: dict[str, str],
    ) -> FitDecision:
        return self._fit_decision

    def rank_discovery_jobs(
        self,
        jobs: list[Any],
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
        preferences: dict[str, Any] | None = None,
    ) -> list[tuple[Any, DiscoveryScore]]:
        ranked: list[tuple[Any, DiscoveryScore]] = []
        for job in jobs:
            ranked.append(
                (
                    job,
                    DiscoveryScore(
                        skill_match_score=70,
                        experience_match_score=75,
                        constraint_score=80,
                        applyability_score=70,
                        priority_score=74,
                        hard_reject=False,
                        reasoning="stub",
                    ),
                )
            )
        return ranked


def _make_settings(base_dir: Path) -> Settings:
    return Settings(
        base_dir=base_dir,
        openai_api_key="",
        openai_model="gpt-4.1-mini",
        cv_path=base_dir / "dummy_cv.pdf",
        knowledge_path=base_dir / "data" / "knowledge.json",
        job_queue_path=base_dir / "data" / "job_queue.jsonl",
        browser_profile_dir=base_dir / "data" / "browser-profile",
        max_jobs_per_run=5,
        min_fit_score=50,
        auto_submit=True,
        apply_external_forms=True,
        dry_run=False,
        skip_already_applied=True,
        slow_mo_ms=0,
        ai_disclosure_enabled=True,
        ai_disclosure_text="AI assisted application.",
        use_system_chrome_profile=False,
        system_chrome_user_data_dir=None,
        system_chrome_profile_name="Default",
        browser_channel=None,
        profile_bootstrap_path=None,
        profile_prompt_on_start=False,
        always_apply_except_outside_poland=True,
        copilot_mode=True,
        terminal_input_enabled=False,
        copilot_wait_timeout_sec=20,
        copilot_poll_interval_ms=500,
        copilot_auto_skip_on_timeout=True,
        agentic_fallback_max_iterations=2,
        agentic_tool_step_limit=8,
        agentic_tool_timeout_sec=30,
        agentic_blocked_action_tokens=("discard", "delete"),
        agentic_playbook_confidence_threshold=0.6,
        agentic_playbook_min_uses=1,
        discovery_enabled=True,
        discovery_keywords_include=("agentic ai", "python"),
        discovery_keywords_exclude=("onsite only",),
        discovery_locations=("Poland",),
        discovery_remote_only=False,
        discovery_days_back=30,
        discovery_max_results=30,
        discovery_cache_path=base_dir / "data" / "job_discovery_cache.json",
        discovery_cache_ttl_minutes=90,
        job_queue_retry_limit=3,
        job_queue_retry_cooldown_minutes=30,
    )


class E2EScenariosTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        (self.base_dir / "data").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "output").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "dummy_cv.pdf").write_text("dummy", encoding="utf-8")

        self.settings = _make_settings(self.base_dir)
        self.knowledge = KnowledgeStore(self.settings.knowledge_path, interactive_prompts=False)
        self.knowledge.profile.update(
            {
                "full_name": "Mateusz Bursztynski Kostrzewa",
                "city": "Warsaw",
                "country": "Poland",
                "years_of_experience": "10",
            }
        )
        self.knowledge.save()
        self.cv_text = "Agentic AI, Python automation, SEO automation."

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build_bot(self, llm: StubLLM | None = None) -> LinkedInJobApplier:
        return LinkedInJobApplier(
            settings=self.settings,
            knowledge=self.knowledge,
            llm=llm or StubLLM(),
            cv_text=self.cv_text,
            cv_path=self.settings.cv_path or (self.base_dir / "dummy_cv.pdf"),
            run_mode="saved_only",
        )

    def test_easy_apply_simple_scenario(self) -> None:
        bot = self._build_bot()
        job_url = "https://www.linkedin.com/jobs/view/111/"

        bot._read_job_posting = lambda page, url: JobPosting(
            job_id="111",
            url=url,
            title="Senior Python Engineer",
            company="Example Co",
            location="Poland",
            description="Simple easy apply role.",
            apply_mode="easy",
        )
        bot._apply_easy = lambda page, job, cover_letter_path, prefilled_answers: True
        bot._unsave_job = lambda page, url: True

        bot._process_single_job(page=object(), context=object(), job_url=job_url)
        record = self.knowledge.get_application_record(job_url)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "submitted")
        self.assertIn("mode=easy", record["notes"])

    def test_external_dynamic_fields_scenario(self) -> None:
        bot = self._build_bot()
        job_url = "https://www.linkedin.com/jobs/view/222/"

        bot._read_job_posting = lambda page, url: JobPosting(
            job_id="222",
            url=url,
            title="Automation Engineer",
            company="Dynamic Forms Inc",
            location="Poland",
            description="External apply with dynamic fields.",
            apply_mode="external",
        )
        bot._apply_external = lambda context, page, job, cover_letter_path, prefilled_answers: True
        bot._unsave_job = lambda page, url: False

        bot._process_single_job(page=object(), context=object(), job_url=job_url)
        record = self.knowledge.get_application_record(job_url)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "submitted")
        self.assertIn("mode=external", record["notes"])

    def test_external_captcha_login_handoff_scenario(self) -> None:
        bot = self._build_bot()
        job_url = "https://www.linkedin.com/jobs/view/333/"

        bot._read_job_posting = lambda page, url: JobPosting(
            job_id="333",
            url=url,
            title="AI Advisor",
            company="Captcha ATS",
            location="Poland",
            description="External flow requiring captcha/login handoff.",
            apply_mode="external",
        )

        def _blocked_external(context, page, job, cover_letter_path, prefilled_answers):
            bot.last_apply_note = "copilot_handoff_unavailable"
            return False

        bot._apply_external = _blocked_external
        bot._unsave_job = lambda page, url: False

        bot._process_single_job(page=object(), context=object(), job_url=job_url)
        record = self.knowledge.get_application_record(job_url)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "not_submitted")
        self.assertIn("note=copilot_handoff_unavailable", record["notes"])

    def test_discovery_to_queue_to_apply_scenario(self) -> None:
        bot = self._build_bot()

        job_ok = DiscoveryJob(
            job_id="444",
            url="https://www.linkedin.com/jobs/view/444/",
            title="Agentic AI Engineer",
            company="Discovery Labs",
            location="Poland",
            snippet="Easy Apply, Agentic AI, Python",
            source_query="agentic ai",
        )
        job_reject = DiscoveryJob(
            job_id="555",
            url="https://www.linkedin.com/jobs/view/555/",
            title="Onsite Specialist",
            company="Outside Corp",
            location="Germany",
            snippet="On-site only",
            source_query="python",
        )

        bot.discovery.build_queries = lambda **kwargs: [DiscoveryQuery(keywords="agentic ai", location="Poland")]
        bot.discovery.discover_jobs = lambda **kwargs: [job_ok, job_reject]

        def _rank(jobs, profile, known_answers, cv_text, preferences=None):
            return [
                (
                    jobs[0],
                    DiscoveryScore(
                        skill_match_score=88,
                        experience_match_score=84,
                        constraint_score=90,
                        applyability_score=86,
                        priority_score=87,
                        hard_reject=False,
                        reasoning="High match",
                    ),
                ),
                (
                    jobs[1],
                    DiscoveryScore(
                        skill_match_score=20,
                        experience_match_score=20,
                        constraint_score=10,
                        applyability_score=30,
                        priority_score=18,
                        hard_reject=True,
                        reject_reason="Outside preferred geography",
                        reasoning="Hard reject",
                    ),
                ),
            ]

        bot.llm.rank_discovery_jobs = _rank
        summary = bot._run_discovery_pipeline(page=object(), max_results=20)
        self.assertEqual(summary["queries"], 1)
        self.assertEqual(summary["discovered"], 2)
        self.assertEqual(summary["rejected"], 1)

        queued_urls = bot.job_queue.get_top_queued_urls(limit=5, sources={"discovery"})
        self.assertEqual(queued_urls, ["https://www.linkedin.com/jobs/view/444"])

        target_url = queued_urls[0]
        bot._read_job_posting = lambda page, url: JobPosting(
            job_id="444",
            url=url,
            title="Agentic AI Engineer",
            company="Discovery Labs",
            location="Poland",
            description="Easy apply discovery result.",
            apply_mode="easy",
        )
        bot._apply_easy = lambda page, job, cover_letter_path, prefilled_answers: True
        bot._unsave_job = lambda page, url: False

        bot._process_single_job(page=object(), context=object(), job_url=target_url)
        bot._sync_job_queue_with_application_record(target_url)

        queued_after_apply = bot.job_queue.get_top_queued_urls(limit=5, sources={"discovery"})
        self.assertEqual(queued_after_apply, [])


if __name__ == "__main__":
    unittest.main()
