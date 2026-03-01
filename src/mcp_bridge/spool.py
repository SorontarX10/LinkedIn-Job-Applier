from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.models import McpEventEnvelope


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc_str(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    if not str(value).strip():
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return _utc_now()


class McpSpoolStore:
    def __init__(self, path: Path, retry_limit: int, retry_backoff_sec: int) -> None:
        self.path = path
        self.retry_limit = max(1, int(retry_limit))
        self.retry_backoff_sec = max(1, int(retry_backoff_sec))
        self.dead_letter_path = path.with_name(f"{path.stem}.dead_letter{path.suffix}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        if not self.dead_letter_path.exists():
            self.dead_letter_path.write_text("", encoding="utf-8")

    def enqueue(self, envelope: McpEventEnvelope, pending_targets: list[str]) -> None:
        if not pending_targets:
            return
        record = {
            "op": "enqueue",
            "at_utc": _to_utc_str(_utc_now()),
            "event": asdict(envelope),
            "pending_targets": list(sorted(set(pending_targets))),
        }
        self._append_line(self.path, record)

    def pending_items_due(self, *, limit: int = 200) -> list[dict[str, Any]]:
        state = self._build_state()
        now = _utc_now()
        due: list[dict[str, Any]] = []
        for item in state.values():
            if item.get("status") != "pending":
                continue
            next_retry_at = _parse_utc(str(item.get("event", {}).get("next_retry_at_utc", "")))
            if next_retry_at > now:
                continue
            due.append(item)
            if len(due) >= max(1, int(limit)):
                break
        due.sort(key=lambda entry: str(entry.get("event", {}).get("ts_utc", "")))
        return due

    def ack(self, event_id: str) -> None:
        if not event_id.strip():
            return
        self._append_line(
            self.path,
            {
                "op": "ack",
                "at_utc": _to_utc_str(_utc_now()),
                "event_id": event_id.strip(),
            },
        )

    def fail(self, item: dict[str, Any], *, pending_targets: list[str], error: str) -> None:
        event = dict(item.get("event", {}))
        event_id = str(event.get("event_id", "")).strip()
        if not event_id:
            return

        attempt = int(event.get("attempt", 0)) + 1
        event["attempt"] = attempt
        retry_at = _utc_now() + timedelta(seconds=self.retry_backoff_sec * max(1, attempt))
        event["next_retry_at_utc"] = _to_utc_str(retry_at)

        if attempt > self.retry_limit:
            self._append_line(
                self.path,
                {
                    "op": "dead_letter",
                    "at_utc": _to_utc_str(_utc_now()),
                    "event_id": event_id,
                    "error": error[:260],
                },
            )
            self._append_line(
                self.dead_letter_path,
                {
                    "at_utc": _to_utc_str(_utc_now()),
                    "event": event,
                    "pending_targets": list(sorted(set(pending_targets))),
                    "error": error[:260],
                },
            )
            return

        self._append_line(
            self.path,
            {
                "op": "update",
                "at_utc": _to_utc_str(_utc_now()),
                "event": event,
                "pending_targets": list(sorted(set(pending_targets))),
                "error": error[:260],
            },
        )

    def backlog_count(self) -> int:
        state = self._build_state()
        return sum(1 for item in state.values() if item.get("status") == "pending")

    def dead_letter_count(self) -> int:
        count = 0
        try:
            for line in self.dead_letter_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    count += 1
        except Exception:
            return 0
        return count

    def _build_state(self) -> dict[str, dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return state
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            op = str(item.get("op", "")).strip().lower()
            if op == "enqueue":
                event = item.get("event")
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id", "")).strip()
                if not event_id:
                    continue
                state[event_id] = {
                    "event": event,
                    "pending_targets": list(item.get("pending_targets", [])),
                    "status": "pending",
                }
            elif op == "update":
                event = item.get("event")
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id", "")).strip()
                if not event_id:
                    continue
                if event_id not in state:
                    state[event_id] = {
                        "event": event,
                        "pending_targets": [],
                        "status": "pending",
                    }
                state[event_id]["event"] = event
                state[event_id]["pending_targets"] = list(item.get("pending_targets", []))
                state[event_id]["status"] = "pending"
            elif op == "ack":
                event_id = str(item.get("event_id", "")).strip()
                if event_id in state:
                    state[event_id]["status"] = "acked"
            elif op == "dead_letter":
                event_id = str(item.get("event_id", "")).strip()
                if event_id in state:
                    state[event_id]["status"] = "dead_letter"
        return state

    @staticmethod
    def _append_line(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")
