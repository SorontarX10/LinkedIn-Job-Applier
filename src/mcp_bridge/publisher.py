from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.config import Settings
from src.mcp_bridge.client import McpStdioClient
from src.mcp_bridge.redaction import redact_payload
from src.mcp_bridge.runtime_config import McpRuntimeConfig, load_runtime_config
from src.mcp_bridge.sinks import FigmaSink, LinearSink, NotionSink
from src.mcp_bridge.spool import McpSpoolStore
from src.models import McpEventEnvelope


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class McpEventPublisher:
    def __init__(
        self,
        *,
        settings: Settings,
        run_id: str,
        snapshot_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self.settings = settings
        self.run_id = run_id
        self.snapshot_provider = snapshot_provider
        self.runtime_config: McpRuntimeConfig = McpRuntimeConfig.defaults()
        self._clients: dict[str, McpStdioClient] = {}
        self._sinks: dict[str, Any] = {}
        self._spool = McpSpoolStore(
            path=self.settings.mcp_spool_path,
            retry_limit=self.settings.mcp_retry_limit,
            retry_backoff_sec=self.settings.mcp_retry_backoff_sec,
        )
        self._started = False
        self.publish_success_count = 0
        self.publish_fail_count = 0

    def start(self) -> None:
        if self._started:
            return
        if not self.settings.mcp_enabled:
            return

        self.runtime_config = load_runtime_config(self.settings.mcp_config_path)
        self._start_clients()
        self._build_sinks()
        self._started = True
        self.drain_spool(max_items=120)

    def stop(self) -> None:
        for client in self._clients.values():
            try:
                client.stop()
            except Exception:
                continue
        self._clients.clear()
        self._started = False

    def publish_event(self, *, event_type: str, payload: dict[str, Any]) -> bool:
        if not self.settings.mcp_enabled:
            return True

        envelope = McpEventEnvelope(
            event_id=str(uuid.uuid4()),
            run_id=self.run_id,
            event_type=str(event_type).strip(),
            ts_utc=_utc_now_str(),
            payload=redact_payload(payload) if self.settings.mcp_redact_pii else payload,
            attempt=0,
            next_retry_at_utc="",
        )
        return self._publish_envelope(envelope=envelope, from_spool=False)

    def drain_spool(self, *, max_items: int = 200) -> None:
        if not self.settings.mcp_enabled:
            return
        due_items = self._spool.pending_items_due(limit=max_items)
        for item in due_items:
            event_raw = item.get("event")
            if not isinstance(event_raw, dict):
                continue
            try:
                envelope = McpEventEnvelope(**event_raw)
            except Exception:
                continue
            self._publish_envelope(envelope=envelope, from_spool=True, pending_targets=list(item.get("pending_targets", [])))

    def stats(self) -> dict[str, int]:
        return {
            "mcp_publish_success": int(self.publish_success_count),
            "mcp_publish_fail": int(self.publish_fail_count),
            "mcp_spool_backlog": int(self._spool.backlog_count()),
            "mcp_dead_letter_count": int(self._spool.dead_letter_count()),
        }

    def _publish_envelope(
        self,
        *,
        envelope: McpEventEnvelope,
        from_spool: bool,
        pending_targets: list[str] | None = None,
    ) -> bool:
        if not self._started:
            self.start()

        if not self._sinks:
            self.publish_fail_count += 1
            if self.settings.mcp_fail_open:
                targets = pending_targets or []
                if not targets:
                    targets = self._enabled_sink_names()
                self._spool.enqueue(envelope, pending_targets=targets)
                return False
            raise RuntimeError("MCP enabled but no active sinks configured.")

        targets = pending_targets or self._enabled_sink_names()
        failed_targets: list[str] = []
        snapshot = self.snapshot_provider()
        for sink_name in targets:
            sink = self._sinks.get(sink_name)
            if sink is None:
                failed_targets.append(sink_name)
                continue
            ok, error = sink.publish(envelope, snapshot)
            if not ok:
                failed_targets.append(sink_name)

        if not failed_targets:
            self.publish_success_count += 1
            if from_spool:
                self._spool.ack(envelope.event_id)
            return True

        self.publish_fail_count += 1
        if from_spool:
            self._spool.fail(
                {"event": asdict(envelope)},
                pending_targets=failed_targets,
                error=";".join(failed_targets),
            )
        else:
            envelope.attempt = 1
            self._spool.enqueue(envelope, pending_targets=failed_targets)
        return False

    def _enabled_sink_names(self) -> list[str]:
        if self._sinks:
            return sorted(self._sinks.keys())
        names: list[str] = []
        if (
            self.settings.mcp_linear_enabled
            and self.runtime_config.linear.enabled
            and self.settings.mcp_linear_team.strip()
            and self.settings.mcp_linear_project.strip()
        ):
            names.append("linear")
        if (
            self.settings.mcp_notion_enabled
            and self.runtime_config.notion.enabled
            and (self.settings.mcp_notion_data_source_id.strip() or self.settings.mcp_notion_parent_page_id.strip())
        ):
            names.append("notion")
        if self.settings.mcp_figma_enabled and self.runtime_config.figma.enabled:
            names.append("figma")
        return names

    def _start_clients(self) -> None:
        sink_servers = {
            self.runtime_config.linear.server,
            self.runtime_config.notion.server,
            self.runtime_config.figma.server,
        }
        for server_name in sink_servers:
            server_cfg = self.runtime_config.servers.get(server_name)
            if server_cfg is None or not server_cfg.enabled:
                continue
            client = McpStdioClient(
                command=server_cfg.command,
                args=server_cfg.args,
                env=server_cfg.env,
                timeout_sec=self.settings.mcp_publish_timeout_sec,
            )
            try:
                client.start()
            except Exception:
                if not self.settings.mcp_fail_open:
                    raise
                continue
            self._clients[server_name] = client

    def _build_sinks(self) -> None:
        self._sinks = {}
        if self.settings.mcp_linear_enabled and self.runtime_config.linear.enabled:
            linear_client = self._clients.get(self.runtime_config.linear.server)
            if (
                linear_client is not None
                and self.settings.mcp_linear_team.strip()
                and self.settings.mcp_linear_project.strip()
            ):
                self._sinks["linear"] = LinearSink(
                    tool_call=self._make_tool_call(linear_client),
                    team=self.settings.mcp_linear_team,
                    project=self.settings.mcp_linear_project,
                    default_state=self.settings.mcp_linear_default_state,
                    create_issue_tool=self.runtime_config.linear.tools.create_issue,
                    create_comment_tool=self.runtime_config.linear.tools.create_comment,
                    update_issue_tool=self.runtime_config.linear.tools.update_issue,
                    issue_map_path=self.settings.base_dir / "data" / "mcp_linear_issue_map.json",
                )
        if self.settings.mcp_notion_enabled and self.runtime_config.notion.enabled:
            notion_client = self._clients.get(self.runtime_config.notion.server)
            if (
                notion_client is not None
                and (self.settings.mcp_notion_data_source_id.strip() or self.settings.mcp_notion_parent_page_id.strip())
            ):
                self._sinks["notion"] = NotionSink(
                    tool_call=self._make_tool_call(notion_client),
                    data_source_id=self.settings.mcp_notion_data_source_id,
                    parent_page_id=self.settings.mcp_notion_parent_page_id,
                    create_page_tool=self.runtime_config.notion.tools.create_page,
                    update_page_tool=self.runtime_config.notion.tools.update_page,
                    run_map_path=self.settings.base_dir / "data" / "mcp_notion_run_map.json",
                )
        if self.settings.mcp_figma_enabled and self.runtime_config.figma.enabled:
            figma_client = self._clients.get(self.runtime_config.figma.server)
            if figma_client is not None:
                self._sinks["figma"] = FigmaSink(
                    tool_call=self._make_tool_call(figma_client),
                    generate_diagram_tool=self.runtime_config.figma.tools.generate_diagram,
                    file_key=self.settings.mcp_figma_file_key,
                )

    @staticmethod
    def _make_tool_call(
        client: McpStdioClient,
    ) -> Callable[[str, dict[str, Any]], tuple[bool, dict[str, Any], str]]:
        def _call(tool_name: str, arguments: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
            response = client.call_tool(tool_name, arguments)
            if not response.ok:
                return False, {}, response.error
            return True, response.result, ""

        return _call


def drain_spool_once(settings: Settings) -> dict[str, int]:
    publisher = McpEventPublisher(
        settings=settings,
        run_id=f"drain-{uuid.uuid4().hex[:8]}",
        snapshot_provider=lambda: {
            "mode": "mcp_drain_only",
            "totals": {},
            "kpi": {},
            "metrics_report_path": "",
        },
    )
    publisher.start()
    publisher.drain_spool(max_items=500)
    stats = publisher.stats()
    publisher.stop()
    return stats
