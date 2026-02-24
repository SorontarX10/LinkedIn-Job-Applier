from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from src.models import ApplicationRecord


def _normalize(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9 ]+", "", value)
    return value


PROFILE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "full_name": ("full name", "name", "imie nazwisko", "your name"),
    "first_name": ("first name", "imie"),
    "last_name": ("last name", "nazwisko"),
    "email": ("email", "e-mail", "mail"),
    "phone": ("phone", "telefon", "mobile"),
    "city": ("city", "miasto", "location"),
    "country": ("country", "kraj"),
    "linkedin_url": ("linkedin", "linkedin profile", "url profilu"),
    "github_url": ("github", "git hub"),
    "portfolio_url": ("portfolio", "website", "strona"),
    "current_title": ("current title", "stanowisko", "job title"),
    "years_of_experience": ("years of experience", "lata doswiadczenia", "experience"),
    "expected_salary": ("salary", "wynagrodzenie", "compensation"),
    "notice_period": ("notice period", "okres wypowiedzenia"),
    "work_authorization": ("work authorization", "visa", "sponsorship", "prawo do pracy"),
}


class KnowledgeStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {
            "profile": {},
            "field_answers": {},
            "applications": {},
        }
        self._load()

    @property
    def profile(self) -> dict[str, str]:
        return self.data["profile"]

    @property
    def field_answers(self) -> dict[str, str]:
        return self.data["field_answers"]

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            self.data["profile"] = raw.get("profile", {}) or {}
            self.data["field_answers"] = raw.get("field_answers", {}) or {}
            self.data["applications"] = raw.get("applications", {}) or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def ensure_profile_fields(self, required: dict[str, str]) -> None:
        for key, prompt in required.items():
            if self.profile.get(key):
                continue
            value = input(f"{prompt}: ").strip()
            if value:
                self.profile[key] = value
        self.save()

    def remember_answer(self, field_label: str, value: str) -> None:
        normalized_label = _normalize(field_label)
        self.field_answers[normalized_label] = value.strip()
        self.save()

    def _match_profile_field(self, normalized_label: str) -> str | None:
        for profile_key, keywords in PROFILE_KEYWORDS.items():
            for keyword in keywords:
                if _normalize(keyword) in normalized_label:
                    return profile_key
        return None

    def get_known_answer(self, field_label: str) -> str:
        normalized_label = _normalize(field_label)
        answer = self.field_answers.get(normalized_label, "").strip()
        if answer:
            return answer

        mapped_profile_key = self._match_profile_field(normalized_label)
        if mapped_profile_key:
            profile_answer = self.profile.get(mapped_profile_key, "").strip()
            if profile_answer:
                return profile_answer
        return ""

    def get_or_ask_answer(self, field_label: str, required: bool = False) -> str:
        existing = self.get_known_answer(field_label)
        if existing:
            return existing

        should_prompt = required or any(
            token in _normalize(field_label)
            for token in (
                "experience",
                "salary",
                "sponsorship",
                "authorization",
                "notice",
                "portfolio",
                "github",
                "linkedin",
                "phone",
                "email",
            )
        )

        if not should_prompt:
            return ""

        value = input(f"[Missing] {field_label}: ").strip()
        if value:
            self.remember_answer(field_label, value)
        return value

    def was_already_applied(self, job_url: str) -> bool:
        normalized = job_url.strip()
        return normalized in self.data["applications"]

    def save_application(self, record: ApplicationRecord) -> None:
        self.data["applications"][record.url] = asdict(record)
        self.save()
