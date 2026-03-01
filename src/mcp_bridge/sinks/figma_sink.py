from __future__ import annotations

from typing import Any, Callable

from src.models import McpEventEnvelope


class FigmaSink:
    def __init__(
        self,
        *,
        tool_call: Callable[[str, dict[str, Any]], tuple[bool, dict[str, Any], str]],
        generate_diagram_tool: str,
        file_key: str,
        min_stuck_events: int = 3,
    ) -> None:
        self.tool_call = tool_call
        self.generate_diagram_tool = generate_diagram_tool
        self.file_key = file_key.strip()
        self.min_stuck_events = max(1, int(min_stuck_events))

    def publish(self, envelope: McpEventEnvelope, snapshot: dict[str, Any]) -> tuple[bool, str]:
        if envelope.event_type != "run_finished":
            return True, ""
        fallback_total = int(snapshot.get("totals", {}).get("fallback_trigger_count", 0))
        if fallback_total < self.min_stuck_events:
            return True, ""

        mermaid = self._build_mermaid(snapshot=snapshot)
        payload = {
            "name": f"LinkedIn Copilot Run {envelope.run_id}",
            "mermaidSyntax": mermaid,
            "userIntent": "Visualize fallback and handoff flow for current run.",
        }
        ok, _, err = self.tool_call(self.generate_diagram_tool, payload)
        if not ok:
            return False, f"figma_diagram_failed:{err}"
        return True, ""

    @staticmethod
    def _build_mermaid(snapshot: dict[str, Any]) -> str:
        totals = snapshot.get("totals", {})
        processed = int(totals.get("processed_jobs", 0))
        fallback = int(totals.get("fallback_trigger_count", 0))
        handoff = int(totals.get("human_handoff_count", 0))
        submitted = int(totals.get("submitted", 0))
        errors = int(totals.get("errors", 0))
        return (
            'flowchart LR\n'
            f'  A["Processed: {processed}"] --> B["Fallback: {fallback}"]\n'
            f'  B --> C["Human Handoff: {handoff}"]\n'
            f'  B --> D["Submitted: {submitted}"]\n'
            f'  B --> E["Errors: {errors}"]\n'
        )

