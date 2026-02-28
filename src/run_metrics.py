from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class _JobTiming:
    source: str
    started_monotonic: float


@dataclass
class RunMetricsTracker:
    base_dir: Path
    mode: str
    run_started_at_utc: str = field(default_factory=_utc_now)
    run_finished_at_utc: str = ""
    queue_selected_count: int = 0
    queue_sources: list[str] = field(default_factory=list)
    processed_jobs: int = 0
    submitted_count: int = 0
    not_submitted_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    fallback_trigger_count: int = 0
    fallback_recovery_success_count: int = 0
    fallback_steps_total: int = 0
    playbook_hit_count: int = 0
    human_handoff_count: int = 0
    discovery_queries_count: int = 0
    discovery_discovered_count: int = 0
    discovery_queued_count: int = 0
    discovery_rejected_count: int = 0
    discovery_submitted_count: int = 0
    handoff_events: list[dict[str, str]] = field(default_factory=list)
    fallback_events: list[dict[str, Any]] = field(default_factory=list)
    job_events: list[dict[str, Any]] = field(default_factory=list)
    _job_timers: dict[str, _JobTiming] = field(default_factory=dict, repr=False)

    def set_queue_context(self, *, selected_count: int, sources: set[str]) -> None:
        self.queue_selected_count = max(0, int(selected_count))
        self.queue_sources = sorted({str(source).strip().lower() for source in sources if str(source).strip()})

    def record_discovery_summary(self, summary: dict[str, int]) -> None:
        self.discovery_queries_count += max(0, int(summary.get("queries", 0)))
        self.discovery_discovered_count += max(0, int(summary.get("discovered", 0)))
        self.discovery_queued_count += max(0, int(summary.get("queued", 0)))
        self.discovery_rejected_count += max(0, int(summary.get("rejected", 0)))

    def start_job(self, *, job_url: str, source: str, started_monotonic: float) -> None:
        normalized_url = str(job_url).strip()
        if not normalized_url:
            return
        self._job_timers[normalized_url] = _JobTiming(source=str(source).strip() or "unknown", started_monotonic=started_monotonic)

    def finish_job(
        self,
        *,
        job_url: str,
        status: str,
        notes: str,
        ended_monotonic: float,
    ) -> None:
        normalized_url = str(job_url).strip()
        normalized_status = str(status).strip().lower() or "unknown"
        timer = self._job_timers.pop(normalized_url, None)
        source = timer.source if timer is not None else "unknown"
        duration_sec = 0.0
        if timer is not None:
            duration_sec = max(0.0, float(ended_monotonic) - float(timer.started_monotonic))

        self.processed_jobs += 1
        if normalized_status == "submitted":
            self.submitted_count += 1
            if source == "discovery":
                self.discovery_submitted_count += 1
        elif normalized_status == "not_submitted":
            self.not_submitted_count += 1
        elif normalized_status.startswith("skipped") or normalized_status == "dry_run_ready":
            self.skipped_count += 1
        elif normalized_status == "error":
            self.error_count += 1
        else:
            self.skipped_count += 1

        self.job_events.append(
            {
                "url": normalized_url,
                "source": source,
                "status": normalized_status,
                "duration_sec": round(duration_sec, 3),
                "notes": str(notes).strip()[:260],
            }
        )

    def record_fallback_trigger(self, *, stage: str) -> None:
        self.fallback_trigger_count += 1
        self.fallback_events.append(
            {
                "event": "trigger",
                "stage": str(stage).strip()[:120],
                "at_utc": _utc_now(),
            }
        )

    def record_fallback_outcome(
        self,
        *,
        stage: str,
        result: str,
        playbook_source: str,
        tool_steps_used: int,
    ) -> None:
        normalized_result = str(result).strip().lower()
        normalized_source = str(playbook_source).strip().lower()
        steps = max(0, int(tool_steps_used))
        self.fallback_steps_total += steps
        if normalized_result in {"clicked", "retry"}:
            self.fallback_recovery_success_count += 1
        if normalized_source == "memory":
            self.playbook_hit_count += 1
        self.fallback_events.append(
            {
                "event": "outcome",
                "stage": str(stage).strip()[:120],
                "result": normalized_result,
                "playbook_source": normalized_source,
                "tool_steps_used": steps,
                "at_utc": _utc_now(),
            }
        )

    def record_human_handoff(self, *, stage: str, result: str) -> None:
        self.human_handoff_count += 1
        self.handoff_events.append(
            {
                "stage": str(stage).strip()[:120],
                "result": str(result).strip().lower()[:80],
                "at_utc": _utc_now(),
            }
        )

    def finalize_and_save(self) -> Path | None:
        self.run_finished_at_utc = _utc_now()
        metrics_payload = self._build_payload()
        out_dir = self.base_dir / "output" / "metrics"
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = self.run_started_at_utc.replace(":", "-")
        report_path = out_dir / f"run_{run_id}.json"
        latest_path = out_dir / "latest.json"
        history_path = out_dir / "runs.jsonl"
        try:
            report_path.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            latest_path.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            with history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(metrics_payload, ensure_ascii=False))
                handle.write("\n")
        except Exception:
            return None
        return report_path

    def _build_payload(self) -> dict[str, Any]:
        mean_time = 0.0
        if self.job_events:
            mean_time = sum(float(item.get("duration_sec", 0.0)) for item in self.job_events) / len(self.job_events)

        mean_steps = 0.0
        if self.processed_jobs > 0:
            mean_steps = float(self.fallback_steps_total) / float(self.processed_jobs)

        application_success_rate = 0.0
        if self.processed_jobs > 0:
            application_success_rate = float(self.submitted_count) / float(self.processed_jobs)

        fallback_trigger_rate = 0.0
        if self.processed_jobs > 0:
            fallback_trigger_rate = float(self.fallback_trigger_count) / float(self.processed_jobs)

        fallback_recovery_success_rate = 0.0
        if self.fallback_trigger_count > 0:
            fallback_recovery_success_rate = float(self.fallback_recovery_success_count) / float(self.fallback_trigger_count)

        human_handoff_rate = 0.0
        if self.processed_jobs > 0:
            human_handoff_rate = float(self.human_handoff_count) / float(self.processed_jobs)

        playbook_hit_rate = 0.0
        if self.fallback_trigger_count > 0:
            playbook_hit_rate = float(self.playbook_hit_count) / float(self.fallback_trigger_count)

        discovery_to_apply_conversion = 0.0
        if self.discovery_queued_count > 0:
            discovery_to_apply_conversion = float(self.discovery_submitted_count) / float(self.discovery_queued_count)

        return {
            "run_started_at_utc": self.run_started_at_utc,
            "run_finished_at_utc": self.run_finished_at_utc,
            "mode": self.mode,
            "queue_selected_count": self.queue_selected_count,
            "queue_sources": self.queue_sources,
            "totals": {
                "processed_jobs": self.processed_jobs,
                "submitted": self.submitted_count,
                "not_submitted": self.not_submitted_count,
                "skipped": self.skipped_count,
                "errors": self.error_count,
                "fallback_trigger_count": self.fallback_trigger_count,
                "fallback_recovery_success_count": self.fallback_recovery_success_count,
                "fallback_steps_total": self.fallback_steps_total,
                "human_handoff_count": self.human_handoff_count,
                "playbook_hit_count": self.playbook_hit_count,
                "discovery_queries_count": self.discovery_queries_count,
                "discovery_discovered_count": self.discovery_discovered_count,
                "discovery_queued_count": self.discovery_queued_count,
                "discovery_rejected_count": self.discovery_rejected_count,
                "discovery_submitted_count": self.discovery_submitted_count,
            },
            "kpi": {
                "application_success_rate": round(application_success_rate, 4),
                "fallback_trigger_rate": round(fallback_trigger_rate, 4),
                "fallback_recovery_success_rate": round(fallback_recovery_success_rate, 4),
                "human_handoff_rate": round(human_handoff_rate, 4),
                "mean_steps_per_application": round(mean_steps, 3),
                "mean_time_per_application_sec": round(mean_time, 3),
                "playbook_hit_rate": round(playbook_hit_rate, 4),
                "discovery_to_apply_conversion": round(discovery_to_apply_conversion, 4),
            },
            "job_events": self.job_events[-300:],
            "fallback_events": self.fallback_events[-300:],
            "handoff_events": self.handoff_events[-300:],
        }
