from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class JobQueueStore:
    def __init__(
        self,
        path: Path,
        *,
        retry_limit: int = 3,
        retry_cooldown_minutes: int = 30,
    ):
        self.path = path
        self.retry_limit = max(1, int(retry_limit))
        self.retry_cooldown_minutes = max(0, int(retry_cooldown_minutes))
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def enqueue_saved_jobs(self, job_urls: list[str]) -> int:
        changed = 0
        for raw_url in job_urls:
            url = self._normalize_url(raw_url)
            if not url:
                continue
            existing = self._items.get(url)
            if existing and str(existing.get("status", "")).strip().lower() == "submitted":
                continue
            self._upsert(
                url=url,
                source="saved",
                status="queued",
            )
            changed += 1
        if changed:
            self._save()
        return changed

    def enqueue_discovery_jobs(self, jobs: list[dict[str, Any]]) -> int:
        changed = 0
        for job in jobs:
            if not isinstance(job, dict):
                continue
            url = self._normalize_url(str(job.get("url", "")))
            if not url:
                continue

            existing = self._items.get(url)
            if existing and str(existing.get("status", "")).strip().lower() == "submitted":
                continue

            score_value = self._safe_int(job.get("score", 0))
            hard_reject = bool(job.get("hard_reject", False))
            status = "rejected" if hard_reject else "queued"
            reason = str(job.get("reason", "")).strip()
            notes = reason[:300]
            source = str(job.get("source", "")).strip() or "discovery"

            self._upsert(
                url=url,
                source=source,
                status=status,
                job_id=str(job.get("job_id", "")).strip() or self._extract_job_id(url),
                score=score_value,
                title=str(job.get("title", "")).strip(),
                company=str(job.get("company", "")).strip(),
                location=str(job.get("location", "")).strip(),
                notes=notes,
                last_status=status,
            )
            changed += 1
        if changed:
            self._save()
        return changed

    def mark_in_progress(self, raw_url: str) -> None:
        url = self._normalize_url(raw_url)
        if not url:
            return
        now_utc = self._now_utc()
        existing = self._items.get(url, {})
        attempts = int(existing.get("attempt_count", 0)) + 1
        self._upsert(
            url=url,
            source=str(existing.get("source", "saved") or "saved"),
            status="in_progress",
            attempt_count=attempts,
            last_attempt_at_utc=now_utc,
        )
        self._save()

    def sync_from_application_record(self, raw_url: str, record: dict[str, Any] | None, source: str = "") -> None:
        url = self._normalize_url(raw_url)
        if not url:
            return
        if not isinstance(record, dict):
            return

        status = str(record.get("status", "")).strip().lower()
        title = str(record.get("title", "")).strip()
        company = str(record.get("company", "")).strip()
        notes = str(record.get("notes", "")).strip()
        score = self._extract_score_from_notes(notes)

        if status == "not_submitted":
            existing = self._items.get(url, {})
            retries = int(existing.get("retry_count", 0)) + 1
            self._upsert(
                url=url,
                source=source,
                status="queued",
                title=title,
                company=company,
                notes=notes,
                last_status="not_submitted",
                retry_count=retries,
                score=score if score is not None else existing.get("score", 0),
            )
            self._save()
            return

        normalized_status = status or "unknown"
        self._upsert(
            url=url,
            source=source,
            status=normalized_status,
            title=title,
            company=company,
            notes=notes,
            last_status=normalized_status,
            score=score if score is not None else self._items.get(url, {}).get("score", 0),
        )
        self._save()

    def get_record(self, raw_url: str) -> dict[str, Any] | None:
        url = self._normalize_url(raw_url)
        if not url:
            return None
        record = self._items.get(url)
        if not isinstance(record, dict):
            return None
        return dict(record)

    def get_top_queued_urls(self, limit: int, sources: set[str] | None = None) -> list[str]:
        capped = max(1, int(limit))
        source_filter = {str(source).strip().lower() for source in (sources or set()) if str(source).strip()}
        now_utc = datetime.now(timezone.utc)
        queued_records = [
            item
            for item in self._items.values()
            if str(item.get("status", "")).strip().lower() == "queued"
            and (not source_filter or str(item.get("source", "")).strip().lower() in source_filter)
            and self._passes_retry_policy(item=item, now_utc=now_utc)
        ]
        queued_records.sort(
            key=lambda item: (
                int(item.get("score", 0)),
                -int(item.get("retry_count", 0)),
                str(item.get("updated_at_utc", "")),
            ),
            reverse=True,
        )
        return [str(item.get("url", "")).strip() for item in queued_records[:capped] if str(item.get("url", "")).strip()]

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            url = self._normalize_url(str(data.get("url", "")))
            if not url:
                continue
            normalized = self._normalize_record(data, url=url)
            self._items[url] = normalized

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records = list(self._items.values())
        records.sort(
            key=lambda item: (
                str(item.get("status", "")),
                -int(item.get("score", 0)),
                str(item.get("updated_at_utc", "")),
            )
        )
        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in records)
        if payload:
            payload += "\n"
        self.path.write_text(payload, encoding="utf-8")

    def _upsert(self, *, url: str, source: str, status: str, **overrides: Any) -> dict[str, Any]:
        now_utc = self._now_utc()
        existing = self._items.get(url, {})
        target_source = source.strip() or str(existing.get("source", "")).strip() or "saved"
        target_status = status.strip() or str(existing.get("status", "")).strip() or "queued"
        base: dict[str, Any] = {
            "job_id": str(existing.get("job_id", "")).strip() or self._extract_job_id(url),
            "url": url,
            "source": target_source,
            "score": int(existing.get("score", 0)),
            "status": target_status,
            "title": str(existing.get("title", "")).strip(),
            "company": str(existing.get("company", "")).strip(),
            "location": str(existing.get("location", "")).strip(),
            "notes": str(existing.get("notes", "")).strip(),
            "last_status": str(existing.get("last_status", "")).strip(),
            "attempt_count": int(existing.get("attempt_count", 0)),
            "retry_count": int(existing.get("retry_count", 0)),
            "last_attempt_at_utc": str(existing.get("last_attempt_at_utc", "")).strip(),
            "created_at_utc": str(existing.get("created_at_utc", "")).strip() or now_utc,
            "updated_at_utc": now_utc,
        }

        for key, value in overrides.items():
            if key in {"score", "attempt_count", "retry_count"}:
                try:
                    base[key] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            elif key in {
                "job_id",
                "source",
                "status",
                "title",
                "company",
                "location",
                "notes",
                "last_status",
                "last_attempt_at_utc",
                "created_at_utc",
                "updated_at_utc",
            }:
                base[key] = str(value).strip()

        base["url"] = url
        base["source"] = base.get("source", "") or target_source
        base["status"] = base.get("status", "") or target_status
        base["updated_at_utc"] = now_utc
        if not base.get("job_id"):
            base["job_id"] = self._extract_job_id(url)

        self._items[url] = base
        return base

    def _normalize_record(self, data: dict[str, Any], *, url: str) -> dict[str, Any]:
        existing_source = str(data.get("source", "")).strip() or "saved"
        existing_status = str(data.get("status", "")).strip() or "queued"
        normalized = {
            "job_id": str(data.get("job_id", "")).strip() or self._extract_job_id(url),
            "url": url,
            "source": existing_source,
            "score": self._safe_int(data.get("score", 0)),
            "status": existing_status,
            "title": str(data.get("title", "")).strip(),
            "company": str(data.get("company", "")).strip(),
            "location": str(data.get("location", "")).strip(),
            "notes": str(data.get("notes", "")).strip(),
            "last_status": str(data.get("last_status", "")).strip(),
            "attempt_count": self._safe_int(data.get("attempt_count", 0)),
            "retry_count": self._safe_int(data.get("retry_count", 0)),
            "last_attempt_at_utc": str(data.get("last_attempt_at_utc", "")).strip(),
            "created_at_utc": str(data.get("created_at_utc", "")).strip() or self._now_utc(),
            "updated_at_utc": str(data.get("updated_at_utc", "")).strip() or self._now_utc(),
        }
        return normalized

    @staticmethod
    def _extract_score_from_notes(notes: str) -> int | None:
        match = re.search(r"fit\s*=\s*(\d{1,3})", notes, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            score = int(match.group(1))
        except ValueError:
            return None
        return max(0, min(100, score))

    @staticmethod
    def _extract_job_id(url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", url)
        if match:
            return match.group(1)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return digest[:12]

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    @staticmethod
    def _normalize_url(url: str) -> str:
        clean = str(url).split("?", 1)[0].split("#", 1)[0].strip()
        clean = re.sub(r"/+$", "", clean)
        return clean

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _parse_utc(raw_value: str) -> datetime | None:
        value = str(raw_value).strip()
        if not value:
            return None
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _passes_retry_policy(self, item: dict[str, Any], now_utc: datetime) -> bool:
        try:
            retry_count = max(0, int(item.get("retry_count", 0)))
        except (TypeError, ValueError):
            retry_count = 0
        if retry_count >= self.retry_limit:
            return False
        if self.retry_cooldown_minutes <= 0:
            return True
        last_attempt_raw = str(item.get("last_attempt_at_utc", "")).strip()
        last_attempt = self._parse_utc(last_attempt_raw)
        if last_attempt is None:
            return True
        return now_utc >= (last_attempt + timedelta(minutes=self.retry_cooldown_minutes))
