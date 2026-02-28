from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page

from src.agentic_tools import AgenticToolExecutor
from src.form_helper import FormHelper
from src.knowledge_store import KnowledgeStore
from src.llm_agent import LLMJobAgent
from src.models import JobPosting


@dataclass
class AgenticFallbackOutcome:
    result: str
    reason: str = ""
    clicked_label: str = ""
    trace_path: str = ""
    playbook_steps: list[dict[str, Any]] | None = None
    playbook_fingerprint: str = ""
    playbook_source: str = ""
    tool_steps_used: int = 0


class AgenticFallbackController:
    """Controller for LLM-driven fallback actions via safe tool execution."""

    def __init__(
        self,
        llm: LLMJobAgent,
        knowledge: KnowledgeStore,
        base_dir: Path,
        *,
        max_iterations: int = 4,
        tool_step_limit: int = 32,
        tool_timeout_sec: int = 120,
        blocked_action_tokens: tuple[str, ...] | None = None,
        playbook_confidence_threshold: float = 0.60,
        playbook_min_uses: int = 1,
    ) -> None:
        self.llm = llm
        self.knowledge = knowledge
        self.base_dir = base_dir
        self.max_iterations = max(1, int(max_iterations))
        self.playbook_confidence_threshold = max(0.0, min(1.0, float(playbook_confidence_threshold)))
        self.playbook_min_uses = max(1, int(playbook_min_uses))
        self.executor = AgenticToolExecutor(
            max_steps_per_session=max(4, int(tool_step_limit)),
            max_session_seconds=max(20, int(tool_timeout_sec)),
            blocked_action_tokens=blocked_action_tokens,
        )

    def run(
        self,
        *,
        page: Page,
        root: Page | Locator,
        job: JobPosting,
        stage: str,
        state_signature: str,
        helper: FormHelper | None,
        action_history: list[str] | None,
        allow_plain_anchors: bool,
    ) -> AgenticFallbackOutcome:
        self.executor.reset_session()
        trace: list[dict[str, Any]] = []
        playbook_steps: list[dict[str, Any]] = []

        # Try best known playbook first for this exact stuck signature.
        if state_signature.strip():
            memory_outcome = self._try_memory_playbooks(
                page=page,
                root=root,
                stage=stage,
                state_signature=state_signature,
                allow_plain_anchors=allow_plain_anchors,
                helper=helper,
                trace=trace,
            )
            if memory_outcome is not None:
                trace_path = self._save_trace(job=job, stage=stage, trace=trace)
                if trace_path and not memory_outcome.trace_path:
                    memory_outcome.trace_path = str(trace_path)
                return memory_outcome

        for iteration in range(self.max_iterations):
            snapshot_res = self.executor.execute("get_dom_snapshot", page=page)
            actions_res = self.executor.execute(
                "list_actions",
                page=page,
                root=root,
                allow_plain_anchors=allow_plain_anchors,
            )
            fields_res = self.executor.execute("list_form_fields", page=page)
            validation_res = self.executor.execute("read_validation_messages", page=page)
            signal_res = self.executor.execute("detect_login_or_captcha", page=page)

            candidates = self._as_list(actions_res.data.get("actions")) if actions_res.ok else []
            visible_fields = self._as_list(fields_res.data.get("fields")) if fields_res.ok else []
            validation_messages = self._as_str_list(validation_res.data.get("messages")) if validation_res.ok else []
            page_signals = self._build_page_signals(signal_res.data if signal_res.ok else {}, visible_fields, validation_messages)
            html_excerpt = str(snapshot_res.data.get("html_excerpt", "")).strip() if snapshot_res.ok else ""

            strategy, button_id, reason = self.llm.choose_stuck_strategy(
                job=job,
                page_url=page.url,
                stage=stage,
                candidates=candidates,
                visible_fields=visible_fields,
                validation_messages=validation_messages,
                page_signals=page_signals,
                recent_actions=action_history or [],
                html_excerpt=html_excerpt,
            )
            strategy = strategy.strip().lower()

            trace_entry: dict[str, Any] = {
                "iteration": iteration + 1,
                "strategy": strategy,
                "button_id": button_id,
                "reason": reason,
                "candidate_count": len(candidates),
                "validation_count": len(validation_messages),
                "required_unanswered_count": int(page_signals.get("required_unanswered_count", 0)),
                "signals": page_signals,
            }

            if strategy == "click_candidate" and button_id is not None:
                click_res = self.executor.execute("click_action", page=page, candidate_id=int(button_id))
                trace_entry["click_result_ok"] = click_res.ok
                trace_entry["click_error"] = click_res.error
                trace.append(trace_entry)
                playbook_steps.append(
                    {
                        "tool": "click_action",
                        "payload": {
                            "candidate_id": int(button_id),
                            "label": str(click_res.data.get("label", "")).strip() if click_res.ok else "",
                        },
                    }
                )
                if click_res.ok:
                    label = str(click_res.data.get("label", "")).strip()
                    wait_res = self.executor.execute("wait", page=page, ms=900)
                    trace.append(
                        {
                            "iteration": iteration + 1,
                            "tool": "wait",
                            "ok": wait_res.ok,
                            "error": wait_res.error,
                            "ms": wait_res.data.get("ms") if wait_res.ok else None,
                        }
                    )
                    playbook_steps.append(
                        {
                            "tool": "wait",
                            "payload": {"ms": 900},
                        }
                    )
                    trace_path = self._save_trace(job=job, stage=stage, trace=trace)
                    return AgenticFallbackOutcome(
                        result="clicked",
                        reason=reason,
                        clicked_label=label,
                        trace_path=str(trace_path) if trace_path else "",
                        playbook_steps=playbook_steps,
                        playbook_source="llm",
                        tool_steps_used=self.executor.steps_used,
                    )
                continue

            if strategy == "fill_and_retry":
                if helper is not None:
                    try:
                        helper.fill_visible_fields(page)
                    except Exception:
                        pass
                playbook_steps.append({"tool": "fill_visible_fields", "payload": {}})
                wait_res = self.executor.execute("wait", page=page, ms=700)
                trace_entry["tool"] = "fill_and_retry"
                trace_entry["wait_ok"] = wait_res.ok
                trace.append(trace_entry)
                playbook_steps.append({"tool": "wait", "payload": {"ms": 700}})
                trace_path = self._save_trace(job=job, stage=stage, trace=trace)
                return AgenticFallbackOutcome(
                    result="retry",
                    reason=reason,
                    trace_path=str(trace_path) if trace_path else "",
                    playbook_steps=playbook_steps,
                    playbook_source="llm",
                    tool_steps_used=self.executor.steps_used,
                )

            if strategy == "wait_and_retry":
                wait_res = self.executor.execute("wait", page=page, ms=900)
                trace_entry["tool"] = "wait"
                trace_entry["wait_ok"] = wait_res.ok
                trace.append(trace_entry)
                playbook_steps.append({"tool": "wait", "payload": {"ms": 900}})
                trace_path = self._save_trace(job=job, stage=stage, trace=trace)
                return AgenticFallbackOutcome(
                    result="retry",
                    reason=reason,
                    trace_path=str(trace_path) if trace_path else "",
                    playbook_steps=playbook_steps,
                    playbook_source="llm",
                    tool_steps_used=self.executor.steps_used,
                )

            if strategy == "wait_human":
                trace_entry["tool"] = "human"
                trace.append(trace_entry)
                playbook_steps.append({"tool": "human_handoff", "payload": {}})
                trace_path = self._save_trace(job=job, stage=stage, trace=trace)
                return AgenticFallbackOutcome(
                    result="human",
                    reason=reason,
                    trace_path=str(trace_path) if trace_path else "",
                    playbook_steps=playbook_steps,
                    playbook_source="llm",
                    tool_steps_used=self.executor.steps_used,
                )

            trace.append(trace_entry)

        trace_path = self._save_trace(job=job, stage=stage, trace=trace)
        return AgenticFallbackOutcome(
            result="none",
            reason="No successful fallback action.",
            trace_path=str(trace_path) if trace_path else "",
            playbook_steps=playbook_steps,
            playbook_source="llm",
            tool_steps_used=self.executor.steps_used,
        )

    def _try_memory_playbooks(
        self,
        *,
        page: Page,
        root: Page | Locator,
        stage: str,
        state_signature: str,
        allow_plain_anchors: bool,
        helper: FormHelper | None,
        trace: list[dict[str, Any]],
    ) -> AgenticFallbackOutcome | None:
        playbooks = self.knowledge.get_agentic_playbooks(state_signature=state_signature, stage=stage, limit=3)
        if not playbooks:
            return None

        for idx, playbook in enumerate(playbooks, start=1):
            metrics = self._playbook_metrics(playbook)
            confidence = self._playbook_confidence(metrics["success_count"], metrics["fail_count"])
            if metrics["uses"] < self.playbook_min_uses or confidence < self.playbook_confidence_threshold:
                trace.append(
                    {
                        "iteration": 0,
                        "source": "memory",
                        "candidate_playbook_rank": idx,
                        "step_fingerprint": str(playbook.get("step_fingerprint", "")).strip(),
                        "skipped_by_confidence": True,
                        "confidence": round(confidence, 4),
                        "uses": metrics["uses"],
                        "success_count": metrics["success_count"],
                        "fail_count": metrics["fail_count"],
                        "threshold": self.playbook_confidence_threshold,
                        "min_uses": self.playbook_min_uses,
                    }
                )
                continue

            steps = playbook.get("steps", [])
            if not isinstance(steps, list) or not steps:
                continue
            fingerprint = str(playbook.get("step_fingerprint", "")).strip()
            trace.append(
                {
                    "iteration": 0,
                    "source": "memory",
                    "candidate_playbook_rank": idx,
                    "step_fingerprint": fingerprint,
                    "steps_count": len(steps),
                    "confidence": round(confidence, 4),
                    "uses": metrics["uses"],
                }
            )

            clicked_label = ""
            failed = False
            for step in steps[:20]:
                if not isinstance(step, dict):
                    failed = True
                    break
                tool = str(step.get("tool", "")).strip().lower()
                payload = step.get("payload")
                if not isinstance(payload, dict):
                    payload = {}

                if tool == "human_handoff":
                    trace.append(
                        {
                            "iteration": 0,
                            "source": "memory",
                            "tool": "human_handoff",
                            "ok": True,
                        }
                    )
                    return AgenticFallbackOutcome(
                        result="human",
                        reason="Recovered via memorized playbook (human_handoff).",
                        trace_path="",
                        playbook_steps=steps,
                        playbook_fingerprint=fingerprint,
                        playbook_source="memory",
                        tool_steps_used=self.executor.steps_used,
                    )

                if tool == "fill_visible_fields":
                    try:
                        if helper is not None:
                            helper.fill_visible_fields(page)
                        trace.append(
                            {
                                "iteration": 0,
                                "source": "memory",
                                "tool": "fill_visible_fields",
                                "ok": True,
                            }
                        )
                    except Exception as exc:
                        trace.append(
                            {
                                "iteration": 0,
                                "source": "memory",
                                "tool": "fill_visible_fields",
                                "ok": False,
                                "error": str(exc),
                            }
                        )
                        failed = True
                        break
                    continue

                if tool == "click_action":
                    # Rebuild action candidates for current DOM before replay click.
                    actions_res = self.executor.execute(
                        "list_actions",
                        page=page,
                        root=root,
                        allow_plain_anchors=allow_plain_anchors,
                    )
                    if not actions_res.ok:
                        trace.append(
                            {
                                "iteration": 0,
                                "source": "memory",
                                "tool": "list_actions",
                                "ok": False,
                                "error": actions_res.error,
                            }
                        )
                        failed = True
                        break
                    actions = self._as_list(actions_res.data.get("actions"))
                    action_id = self._resolve_action_id(actions, payload)
                    if action_id is None:
                        trace.append(
                            {
                                "iteration": 0,
                                "source": "memory",
                                "tool": "click_action",
                                "ok": False,
                                "error": "No matching action candidate in current DOM.",
                            }
                        )
                        failed = True
                        break
                    click_res = self.executor.execute("click_action", page=page, candidate_id=action_id)
                    trace.append(
                        {
                            "iteration": 0,
                            "source": "memory",
                            "tool": "click_action",
                            "ok": click_res.ok,
                            "error": click_res.error,
                            "candidate_id": action_id,
                            "label": click_res.data.get("label", "") if click_res.ok else "",
                        }
                    )
                    if not click_res.ok:
                        failed = True
                        break
                    clicked_label = str(click_res.data.get("label", "")).strip()
                    continue

                step_res = self.executor.execute(tool, page=page, **payload)
                trace.append(
                    {
                        "iteration": 0,
                        "source": "memory",
                        "tool": tool,
                        "ok": step_res.ok,
                        "error": step_res.error,
                    }
                )
                if not step_res.ok:
                    failed = True
                    break

            if failed:
                if state_signature and fingerprint:
                    self.knowledge.mark_agentic_playbook_result(
                        state_signature=state_signature,
                        stage=stage,
                        step_fingerprint=fingerprint,
                        success=False,
                    )
                continue

            if state_signature and fingerprint:
                self.knowledge.mark_agentic_playbook_result(
                    state_signature=state_signature,
                    stage=stage,
                    step_fingerprint=fingerprint,
                    success=True,
                )
            return AgenticFallbackOutcome(
                result="clicked" if clicked_label else "retry",
                reason="Recovered via memorized playbook.",
                clicked_label=clicked_label,
                trace_path="",
                playbook_steps=steps,
                playbook_fingerprint=fingerprint,
                playbook_source="memory",
                tool_steps_used=self.executor.steps_used,
            )
        return None

    @staticmethod
    def _resolve_action_id(actions: list[dict[str, Any]], payload: dict[str, Any]) -> int | None:
        requested_label = str(payload.get("label", "")).strip()
        requested_id_raw = payload.get("candidate_id")
        requested_id = None
        try:
            requested_id = int(requested_id_raw)
        except Exception:
            requested_id = None

        if requested_label:
            requested_norm = re.sub(r"\s+", " ", requested_label).strip().lower()
            for action in actions:
                try:
                    action_id = int(action.get("id", -1))
                except Exception:
                    continue
                label = str(action.get("label", "")).strip().lower()
                if not label:
                    continue
                if requested_norm == label or requested_norm in label or label in requested_norm:
                    return action_id

        if requested_id is not None:
            for action in actions:
                try:
                    action_id = int(action.get("id", -1))
                except Exception:
                    continue
                if action_id == requested_id:
                    return action_id
        return None

    @staticmethod
    def _playbook_metrics(playbook: dict[str, Any]) -> dict[str, int]:
        success_count = 0
        fail_count = 0
        try:
            success_count = max(0, int(playbook.get("success_count", 0)))
        except Exception:
            success_count = 0
        try:
            fail_count = max(0, int(playbook.get("fail_count", 0)))
        except Exception:
            fail_count = 0
        return {
            "success_count": success_count,
            "fail_count": fail_count,
            "uses": success_count + fail_count,
        }

    @staticmethod
    def _playbook_confidence(success_count: int, fail_count: int) -> float:
        uses = success_count + fail_count
        if uses <= 0:
            return 0.0
        ratio = success_count / uses
        net = max(0.0, (success_count - fail_count) / uses)
        bonus = min(0.15, 0.03 * success_count)
        confidence = (0.75 * ratio) + (0.25 * net) + bonus
        return max(0.0, min(1.0, confidence))

    def _save_trace(self, job: JobPosting, stage: str, trace: list[dict[str, Any]]) -> Path | None:
        if not trace:
            return None
        out_dir = self.base_dir / "output" / "agentic_traces"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "_", stage).strip("_") or "stage"
        out_path = out_dir / f"{job.job_id}_{safe_stage}_{int(time.time())}.json"
        payload = {
            "job_id": job.job_id,
            "job_url": job.url,
            "stage": stage,
            "trace": trace,
        }
        try:
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return out_path
        except Exception:
            return None

    @staticmethod
    def _as_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(item)
        return out

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text[:220])
        return out

    @staticmethod
    def _build_page_signals(
        base_signals: dict[str, Any],
        visible_fields: list[dict[str, Any]],
        validation_messages: list[str],
    ) -> dict[str, Any]:
        required_unanswered: list[str] = []
        for field in visible_fields[:80]:
            label = str(field.get("label", "")).strip()
            if not label:
                continue
            if not bool(field.get("required", False)):
                continue
            current_value = str(field.get("current_value", "")).strip()
            if not current_value:
                required_unanswered.append(label[:140])

        signals = dict(base_signals)
        signals["required_unanswered"] = required_unanswered[:12]
        signals["required_unanswered_count"] = len(required_unanswered)
        signals["validation_messages"] = validation_messages[:12]
        return signals
