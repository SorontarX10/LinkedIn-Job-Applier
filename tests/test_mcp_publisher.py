from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.config import Settings
from src.mcp_bridge.publisher import McpEventPublisher


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
        dry_run=True,
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
        agentic_llm_plan_enabled=True,
        agentic_llm_plan_max_steps=4,
        agentic_primary_after_apply=True,
        discovery_enabled=False,
        discovery_keywords_include=(),
        discovery_keywords_exclude=(),
        discovery_locations=(),
        discovery_remote_only=False,
        discovery_days_back=30,
        discovery_max_results=30,
        discovery_cache_path=base_dir / "data" / "job_discovery_cache.json",
        discovery_cache_ttl_minutes=90,
        job_queue_retry_limit=3,
        job_queue_retry_cooldown_minutes=30,
        mcp_enabled=True,
        mcp_fail_open=True,
        mcp_config_path=base_dir / "data" / "mcp_servers.runtime.json",
        mcp_spool_path=base_dir / "data" / "mcp_spool.jsonl",
        mcp_publish_timeout_sec=3,
        mcp_retry_limit=3,
        mcp_retry_backoff_sec=1,
        mcp_redact_pii=True,
        mcp_linear_enabled=True,
        mcp_linear_team="SOR",
        mcp_linear_project="Copilot",
        mcp_linear_default_state="Backlog",
        mcp_notion_enabled=True,
        mcp_notion_data_source_id="collection://demo",
        mcp_notion_parent_page_id="",
        mcp_figma_enabled=False,
        mcp_figma_file_key="",
    )


class McpPublisherTest(unittest.TestCase):
    def test_publisher_routes_to_linear_and_notion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "data").mkdir(parents=True, exist_ok=True)
            fake_server = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
            runtime_cfg = {
                "servers": {
                    "fake": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(fake_server)],
                        "env": {},
                    }
                },
                "sinks": {
                    "linear": {"enabled": True, "server": "fake"},
                    "notion": {"enabled": True, "server": "fake"},
                    "figma": {"enabled": False, "server": "fake"},
                },
            }
            cfg_path = base_dir / "data" / "mcp_servers.runtime.json"
            cfg_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False, indent=2), encoding="utf-8")

            settings = _make_settings(base_dir)
            publisher = McpEventPublisher(
                settings=settings,
                run_id="run-test-1",
                snapshot_provider=lambda: {
                    "mode": "saved_only",
                    "totals": {"processed_jobs": 1, "submitted": 0, "errors": 1},
                    "kpi": {"application_success_rate": 0.0, "mcp_publish_fail_rate": 0.0},
                    "metrics_report_path": "",
                },
            )
            publisher.start()
            ok_1 = publisher.publish_event(
                event_type="job_processing_error",
                payload={"job_url": "https://www.linkedin.com/jobs/view/123", "stage": "external_submit"},
            )
            ok_2 = publisher.publish_event(
                event_type="run_finished",
                payload={"mode": "saved_only"},
            )
            publisher.stop()

            stats = publisher.stats()
            self.assertTrue(ok_1)
            self.assertTrue(ok_2)
            self.assertGreaterEqual(stats["mcp_publish_success"], 2)
            self.assertEqual(stats["mcp_spool_backlog"], 0)

    def test_timeout_falls_back_to_spool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "data").mkdir(parents=True, exist_ok=True)
            fake_server = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
            runtime_cfg = {
                "servers": {
                    "fake": {
                        "enabled": True,
                        "command": sys.executable,
                        "args": [str(fake_server)],
                        "env": {"FAKE_MCP_SLEEP_SEC": "2"},
                    }
                },
                "sinks": {
                    "linear": {"enabled": True, "server": "fake"},
                    "notion": {"enabled": False, "server": "fake"},
                    "figma": {"enabled": False, "server": "fake"},
                },
            }
            cfg_path = base_dir / "data" / "mcp_servers.runtime.json"
            cfg_path.write_text(json.dumps(runtime_cfg, ensure_ascii=False, indent=2), encoding="utf-8")

            settings = _make_settings(base_dir)
            settings.mcp_publish_timeout_sec = 1
            settings.mcp_notion_enabled = False
            publisher = McpEventPublisher(
                settings=settings,
                run_id="run-test-2",
                snapshot_provider=lambda: {"mode": "saved_only", "totals": {}, "kpi": {}, "metrics_report_path": ""},
            )
            publisher.start()
            ok = publisher.publish_event(
                event_type="job_processing_error",
                payload={"job_url": "https://www.linkedin.com/jobs/view/456", "stage": "external_submit"},
            )
            publisher.stop()

            stats = publisher.stats()
            self.assertFalse(ok)
            self.assertGreaterEqual(stats["mcp_publish_fail"], 1)
            self.assertGreaterEqual(stats["mcp_spool_backlog"], 1)


if __name__ == "__main__":
    unittest.main()

