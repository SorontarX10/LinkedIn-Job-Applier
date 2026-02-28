from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truncate_text(value: str, *, max_len: int = 600) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _sanitize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            sanitized[_truncate_text(str(key), max_len=80)] = _sanitize(item)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in list(value)[:40]]
    return _truncate_text(str(value))


@dataclass
class OperationHistoryTracker:
    base_dir: Path
    mode: str
    run_started_at_utc: str = field(default_factory=_utc_now)
    run_finished_at_utc: str = ""
    run_id: str = field(init=False)
    run_path: Path = field(init=False)
    latest_path: Path = field(init=False)
    _sequence: int = field(default=0, init=False, repr=False)
    _recent_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        out_dir = self.base_dir / "output" / "operations"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = self.run_started_at_utc.replace(":", "-")
        self.run_path = out_dir / f"run_{self.run_id}.jsonl"
        self.latest_path = out_dir / "latest.jsonl"
        try:
            self.run_path.write_text("", encoding="utf-8")
            self.latest_path.write_text("", encoding="utf-8")
        except Exception:
            pass

    def log(self, event: str, **data: Any) -> dict[str, Any]:
        self._sequence += 1
        payload: dict[str, Any] = {
            "seq": self._sequence,
            "at_utc": _utc_now(),
            "event": _truncate_text(str(event).strip() or "event", max_len=80),
            "mode": _truncate_text(self.mode, max_len=40),
        }
        for key, value in data.items():
            normalized_key = _truncate_text(str(key).strip(), max_len=80)
            if not normalized_key:
                continue
            payload[normalized_key] = _sanitize(value)

        line = json.dumps(payload, ensure_ascii=False)
        try:
            with self.run_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
            with self.latest_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
        except Exception:
            pass
        self._recent_events.append(payload)
        if len(self._recent_events) > 1000:
            self._recent_events = self._recent_events[-1000:]
        return payload

    def recent(self, limit: int = 25) -> list[dict[str, Any]]:
        count = max(1, min(200, int(limit)))
        return list(self._recent_events[-count:])

    def finalize(self, **summary: Any) -> None:
        if not self.run_finished_at_utc:
            self.run_finished_at_utc = _utc_now()
        self.log("run_finished", run_finished_at_utc=self.run_finished_at_utc, summary=summary)
