from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.models import McpEventEnvelope


class NotionSink:
    def __init__(
        self,
        *,
        tool_call: Callable[[str, dict[str, Any]], tuple[bool, dict[str, Any], str]],
        data_source_id: str,
        parent_page_id: str,
        create_page_tool: str,
        update_page_tool: str,
        run_map_path: Path,
    ) -> None:
        self.tool_call = tool_call
        self.data_source_id = data_source_id.strip()
        self.parent_page_id = parent_page_id.strip()
        self.create_page_tool = create_page_tool
        self.update_page_tool = update_page_tool
        self.run_map_path = run_map_path
        self.run_map_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    def publish(self, envelope: McpEventEnvelope, snapshot: dict[str, Any]) -> tuple[bool, str]:
        if envelope.event_type != "run_finished":
            return True, ""
        run_id = envelope.run_id.strip()
        page_id = str(self._state.get(run_id, "")).strip()
        if page_id:
            return self._update_page(page_id=page_id, run_id=run_id, snapshot=snapshot)
        return self._create_page(run_id=run_id, snapshot=snapshot)

    def _create_page(self, *, run_id: str, snapshot: dict[str, Any]) -> tuple[bool, str]:
        title = f"LinkedIn Copilot Run {run_id}"
        content = self._snapshot_markdown(run_id=run_id, snapshot=snapshot)
        parent = self.parent_page_id.strip() if self.parent_page_id.strip() else self.data_source_id

        payload = {
            "parent": parent,
            "pages": [
                {
                    "properties": {"title": title[:180]},
                    "content": content,
                }
            ],
        }
        ok, result, err = self.tool_call(self.create_page_tool, payload)
        if not ok:
            return False, f"notion_create_failed:{err}"
        page_id = self._extract_page_id(result)
        if page_id:
            self._state[run_id] = page_id
            self._save_state()
        return True, ""

    def _update_page(self, *, page_id: str, run_id: str, snapshot: dict[str, Any]) -> tuple[bool, str]:
        payload = {
            "page_id": page_id,
            "command": "replace_content",
            "new_str": self._snapshot_markdown(run_id=run_id, snapshot=snapshot),
        }
        ok, _, err = self.tool_call(self.update_page_tool, payload)
        if not ok:
            return False, f"notion_update_failed:{err}"
        return True, ""

    @staticmethod
    def _snapshot_markdown(*, run_id: str, snapshot: dict[str, Any]) -> str:
        totals = snapshot.get("totals", {})
        kpi = snapshot.get("kpi", {})
        lines = [
            f"# Run {run_id}",
            "",
            "## Totals",
            f"- processed_jobs: {totals.get('processed_jobs', 0)}",
            f"- submitted: {totals.get('submitted', 0)}",
            f"- not_submitted: {totals.get('not_submitted', 0)}",
            f"- errors: {totals.get('errors', 0)}",
            f"- fallback_trigger_count: {totals.get('fallback_trigger_count', 0)}",
            f"- human_handoff_count: {totals.get('human_handoff_count', 0)}",
            "",
            "## KPI",
            f"- application_success_rate: {kpi.get('application_success_rate', 0)}",
            f"- fallback_recovery_success_rate: {kpi.get('fallback_recovery_success_rate', 0)}",
            f"- human_handoff_rate: {kpi.get('human_handoff_rate', 0)}",
            f"- mean_time_per_application_sec: {kpi.get('mean_time_per_application_sec', 0)}",
            f"- mcp_publish_fail_rate: {kpi.get('mcp_publish_fail_rate', 0)}",
            "",
            "## Paths",
            f"- metrics_report_path: {snapshot.get('metrics_report_path', '')}",
        ]
        return "\n".join(lines)[:12000]

    @staticmethod
    def _extract_page_id(result: dict[str, Any]) -> str:
        for key in ("id", "page_id", "pageId"):
            value = str(result.get(key, "")).strip()
            if value:
                return value
        return ""

    def _load_state(self) -> dict[str, str]:
        if not self.run_map_path.exists():
            return {}
        try:
            raw = json.loads(self.run_map_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in raw.items():
            text = str(value).strip()
            if text:
                out[str(key)] = text
        return out

    def _save_state(self) -> None:
        self.run_map_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")

