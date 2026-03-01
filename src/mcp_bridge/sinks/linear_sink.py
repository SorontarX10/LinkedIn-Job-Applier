from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.mcp_bridge.redaction import normalize_error_signature, stable_hash
from src.models import McpEventEnvelope


class LinearSink:
    def __init__(
        self,
        *,
        tool_call: Callable[[str, dict[str, Any]], tuple[bool, dict[str, Any], str]],
        team: str,
        project: str,
        default_state: str,
        create_issue_tool: str,
        create_comment_tool: str,
        update_issue_tool: str,
        issue_map_path: Path,
    ) -> None:
        self.tool_call = tool_call
        self.team = team.strip()
        self.project = project.strip()
        self.default_state = default_state.strip() or "Backlog"
        self.create_issue_tool = create_issue_tool
        self.create_comment_tool = create_comment_tool
        self.update_issue_tool = update_issue_tool
        self.issue_map_path = issue_map_path
        self.issue_map_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    def publish(self, envelope: McpEventEnvelope, snapshot: dict[str, Any]) -> tuple[bool, str]:
        event_type = envelope.event_type
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}

        if event_type in {"job_processing_error", "human_handoff_timeout"}:
            return self._handle_incident(envelope, payload, snapshot)
        if event_type in {"job_completion_recorded", "job_processing_completed"}:
            return self._handle_possible_resolution(payload)
        return True, ""

    def _handle_incident(
        self,
        envelope: McpEventEnvelope,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[bool, str]:
        job_url = str(payload.get("job_url", "")).strip()
        stage = str(payload.get("stage", "")).strip() or "general"
        reason = str(payload.get("reason", "")).strip() or str(payload.get("error_text", "")).strip()
        signature = normalize_error_signature(job_url, stage, reason or envelope.event_type)
        dedupe_key = stable_hash(signature)

        issue_entry = self._state.get(dedupe_key, {})
        issue_id = str(issue_entry.get("issue_id", "")).strip()
        if issue_id:
            ok, _, err = self.tool_call(
                self.create_comment_tool,
                {
                    "issueId": issue_id,
                    "body": self._incident_comment(envelope=envelope, payload=payload, snapshot=snapshot),
                },
            )
            if ok:
                return True, ""
            return False, f"linear_comment_failed:{err}"

        title = f"[Copilot] Incident: {stage}"
        if job_url:
            title = f"[Copilot] Incident: {stage} ({job_url.split('/')[-1]})"
        description = self._incident_description(envelope=envelope, payload=payload, snapshot=snapshot)
        ok, result, err = self.tool_call(
            self.create_issue_tool,
            {
                "team": self.team,
                "project": self.project,
                "state": self.default_state,
                "title": title[:200],
                "description": description,
            },
        )
        if not ok:
            return False, f"linear_issue_create_failed:{err}"

        extracted_issue_id = self._extract_issue_id(result)
        if extracted_issue_id:
            self._state[dedupe_key] = {
                "issue_id": extracted_issue_id,
                "job_url": job_url,
                "stage": stage,
            }
            self._save_state()
        return True, ""

    def _handle_possible_resolution(self, payload: dict[str, Any]) -> tuple[bool, str]:
        status = str(payload.get("status", "")).strip().lower()
        if status != "submitted":
            return True, ""
        job_url = str(payload.get("job_url", "")).strip()
        if not job_url:
            return True, ""

        changed = False
        for entry in self._state.values():
            if str(entry.get("job_url", "")).strip() != job_url:
                continue
            issue_id = str(entry.get("issue_id", "")).strip()
            if not issue_id:
                continue
            ok, _, _ = self.tool_call(
                self.update_issue_tool,
                {
                    "id": issue_id,
                    "state": "Done",
                },
            )
            if ok:
                changed = True
        if changed:
            self._save_state()
        return True, ""

    def _incident_description(
        self,
        *,
        envelope: McpEventEnvelope,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> str:
        lines = [
            f"Event: {envelope.event_type}",
            f"Run ID: {envelope.run_id}",
            f"Timestamp: {envelope.ts_utc}",
            f"Job URL: {payload.get('job_url', '')}",
            f"Stage: {payload.get('stage', '')}",
            f"Reason: {payload.get('reason', '') or payload.get('error_text', '')}",
            "",
            "Snapshot:",
            f"- mode: {snapshot.get('mode', '')}",
            f"- processed: {snapshot.get('totals', {}).get('processed_jobs', '')}",
            f"- submitted: {snapshot.get('totals', {}).get('submitted', '')}",
            f"- errors: {snapshot.get('totals', {}).get('errors', '')}",
        ]
        return "\n".join(line[:400] for line in lines if line is not None)

    def _incident_comment(
        self,
        *,
        envelope: McpEventEnvelope,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> str:
        return (
            f"Recurring incident detected.\n"
            f"- Event: {envelope.event_type}\n"
            f"- Run: {envelope.run_id}\n"
            f"- Job URL: {payload.get('job_url', '')}\n"
            f"- Stage: {payload.get('stage', '')}\n"
            f"- Reason: {payload.get('reason', '') or payload.get('error_text', '')}\n"
            f"- Current errors total: {snapshot.get('totals', {}).get('errors', '')}\n"
        )[:3500]

    @staticmethod
    def _extract_issue_id(result: dict[str, Any]) -> str:
        candidates = [
            result.get("id"),
            result.get("issueId"),
            result.get("identifier"),
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value
        return ""

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self.issue_map_path.exists():
            return {}
        try:
            raw = json.loads(self.issue_map_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                out[str(key)] = value
        return out

    def _save_state(self) -> None:
        self.issue_map_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")

