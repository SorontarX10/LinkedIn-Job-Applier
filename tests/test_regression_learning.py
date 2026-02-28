from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from src.agentic_fallback import AgenticFallbackController
from src.agentic_tools import ToolResult
from src.knowledge_store import KnowledgeStore
from src.models import JobPosting


class _StubLLM:
    def choose_stuck_strategy(
        self,
        job: JobPosting,
        page_url: str,
        stage: str,
        candidates,
        visible_fields=None,
        validation_messages=None,
        page_signals=None,
        recent_actions=None,
        html_excerpt: str = "",
    ):
        del job, page_url, stage, candidates, visible_fields, validation_messages, page_signals, recent_actions, html_excerpt
        return "wait_human", None, "manual help required"


class _FakePage:
    def __init__(self, url: str = "https://example.test/form") -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False


class LearningRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        knowledge_path = self.base_dir / "data" / "knowledge.json"
        self.knowledge = KnowledgeStore(knowledge_path, interactive_prompts=False)
        self.controller = AgenticFallbackController(
            llm=_StubLLM(),
            knowledge=self.knowledge,
            base_dir=self.base_dir,
            max_iterations=1,
            tool_step_limit=20,
            tool_timeout_sec=30,
            playbook_confidence_threshold=0.6,
            playbook_min_uses=1,
        )
        self.job = JobPosting(
            job_id="111",
            url="https://www.linkedin.com/jobs/view/111/",
            title="AI Engineer",
            company="Example",
            location="Poland",
            description="Test role",
            apply_mode="external",
        )
        self.page = _FakePage()

        def _fake_execute(self_executor, tool_name: str, **kwargs):
            self_executor._steps_used += 1
            if tool_name == "get_dom_snapshot":
                return ToolResult(ok=True, tool=tool_name, data={"html_excerpt": "<form></form>"})
            if tool_name == "list_actions":
                return ToolResult(
                    ok=True,
                    tool=tool_name,
                    data={
                        "actions": [
                            {"id": "0", "label": "Continue", "role": "button", "type": "button", "href": ""}
                        ]
                    },
                )
            if tool_name == "list_form_fields":
                return ToolResult(ok=True, tool=tool_name, data={"fields": []})
            if tool_name == "read_validation_messages":
                return ToolResult(ok=True, tool=tool_name, data={"messages": []})
            if tool_name == "detect_login_or_captcha":
                return ToolResult(
                    ok=True,
                    tool=tool_name,
                    data={"requires_login": False, "has_hcaptcha": False, "has_recaptcha": False},
                )
            if tool_name == "click_action":
                return ToolResult(
                    ok=True,
                    tool=tool_name,
                    data={"candidate_id": int(kwargs.get("candidate_id", 0)), "label": "Continue"},
                )
            if tool_name == "wait":
                return ToolResult(ok=True, tool=tool_name, data={"ms": int(kwargs.get("ms", 0))})
            return ToolResult(ok=False, tool=tool_name, error=f"unexpected tool {tool_name}")

        self.controller.executor.execute = types.MethodType(_fake_execute, self.controller.executor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run2_uses_playbook_after_run1_handoff_learning(self) -> None:
        state_signature = "sig:external:step1"
        stage = "external_submit"

        run1 = self.controller.run(
            page=self.page,
            root=self.page,
            job=self.job,
            stage=stage,
            state_signature=state_signature,
            helper=None,
            action_history=[],
            allow_plain_anchors=True,
        )
        self.assertEqual(run1.result, "human")

        learned_steps = [
            {"tool": "click_action", "payload": {"candidate_id": 0, "label": "Continue"}},
            {"tool": "wait", "payload": {"ms": 700}},
        ]
        fingerprint = self.knowledge.remember_agentic_playbook(
            state_signature=state_signature,
            stage=stage,
            steps=learned_steps,
            source="human_event_sequence",
            notes="learned from manual intervention",
        )
        self.assertIsNotNone(fingerprint)
        self.knowledge.mark_agentic_playbook_result(
            state_signature=state_signature,
            stage=stage,
            step_fingerprint=str(fingerprint),
            success=True,
        )

        run2 = self.controller.run(
            page=self.page,
            root=self.page,
            job=self.job,
            stage=stage,
            state_signature=state_signature,
            helper=None,
            action_history=[],
            allow_plain_anchors=True,
        )
        self.assertEqual(run2.playbook_source, "memory")
        self.assertEqual(run2.result, "clicked")


if __name__ == "__main__":
    unittest.main()
