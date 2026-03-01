from __future__ import annotations

from typing import Any

from src.mcp_bridge.publisher import McpEventPublisher


class PublisherAdapter:
    def __init__(self, publisher: McpEventPublisher) -> None:
        self.publisher = publisher

    def on_run_started(self, payload: dict[str, Any]) -> None:
        self.publisher.publish_event(event_type="run_started", payload=payload)

    def on_job_started(self, payload: dict[str, Any]) -> None:
        self.publisher.publish_event(event_type="job_processing_started", payload=payload)

    def on_job_finished(self, payload: dict[str, Any]) -> None:
        self.publisher.publish_event(event_type="job_processing_completed", payload=payload)

    def on_error(self, payload: dict[str, Any]) -> None:
        self.publisher.publish_event(event_type="job_processing_error", payload=payload)

    def on_run_finished(self, payload: dict[str, Any]) -> None:
        self.publisher.publish_event(event_type="run_finished", payload=payload)

