from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

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

SENSITIVE_FIELD_TOKENS = (
    "password",
    "passcode",
    "otp",
    "captcha",
    "security code",
    "verification code",
    "2fa",
    "token",
    "one time",
)

NON_ANSWER_FIELD_TOKENS = (
    "consent",
    "privacy",
    "terms",
    "policy",
    "agreement checkbox",
    "newsletter",
    "follow company",
)


def _safe_console_prompt(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except Exception:
        return text


class KnowledgeStore:
    def __init__(self, path: Path, interactive_prompts: bool = False):
        self.path = path
        self.interactive_prompts = interactive_prompts
        self._asked_without_answer: set[str] = set()
        self.data: dict[str, dict] = {
            "profile": {},
            "field_answers": {},
            "applications": {},
            "copilot_recipes": [],
        }
        self._load()

    @property
    def profile(self) -> dict[str, str]:
        return self.data["profile"]

    @property
    def field_answers(self) -> dict[str, str]:
        return self.data["field_answers"]

    @property
    def copilot_recipes(self) -> list[dict[str, Any]]:
        recipes = self.data.get("copilot_recipes")
        if isinstance(recipes, list):
            return recipes
        self.data["copilot_recipes"] = []
        return self.data["copilot_recipes"]

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict):
            self.data["profile"] = raw.get("profile", {}) or {}
            self.data["field_answers"] = raw.get("field_answers", {}) or {}
            self.data["applications"] = raw.get("applications", {}) or {}
            recipes = raw.get("copilot_recipes", [])
            self.data["copilot_recipes"] = recipes if isinstance(recipes, list) else []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _prompts_enabled(self) -> bool:
        if not self.interactive_prompts:
            return False
        return bool(getattr(sys.stdin, "isatty", lambda: False)())

    def ensure_profile_fields(self, required: dict[str, str]) -> None:
        interactive = self._prompts_enabled()
        for key, prompt in required.items():
            if self.profile.get(key):
                continue
            if not interactive:
                continue
            try:
                value = input(_safe_console_prompt(f"{prompt}: ")).strip()
            except EOFError:
                value = ""
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

        normalized_label = _normalize(field_label)
        if normalized_label in self._asked_without_answer:
            return ""

        should_prompt = required or any(
            token in normalized_label
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

        interactive = self._prompts_enabled()
        if not interactive:
            return ""

        compact_label = re.sub(r"\s+", " ", field_label).strip()
        if len(compact_label) > 180:
            compact_label = f"{compact_label[:177].rstrip()}..."

        try:
            value = input(_safe_console_prompt(f"[Missing] {compact_label}: ")).strip()
        except EOFError:
            value = ""
        if value:
            self._asked_without_answer.discard(normalized_label)
            self.remember_answer(field_label, value)
        else:
            self._asked_without_answer.add(normalized_label)
        return value

    def was_already_applied(self, job_url: str) -> bool:
        normalized = job_url.strip()
        return normalized in self.data["applications"]

    def get_application_record(self, job_url: str) -> dict | None:
        normalized = job_url.strip()
        record = self.data["applications"].get(normalized)
        if isinstance(record, dict):
            return record
        return None

    def save_application(self, record: ApplicationRecord) -> None:
        self.data["applications"][record.url] = asdict(record)
        self.save()

    @staticmethod
    def _is_placeholder_value(value: str) -> bool:
        normalized = _normalize(value)
        if not normalized:
            return True
        return normalized in {
            "select",
            "select option",
            "choose",
            "choose option",
            "please select",
            "please choose",
            "n a",
            "na",
            "none",
        }

    def _is_learnable_observation(self, label: str, value: str) -> bool:
        normalized_label = _normalize(label)
        if not normalized_label:
            return False
        if any(token in normalized_label for token in SENSITIVE_FIELD_TOKENS):
            return False
        if any(token in normalized_label for token in NON_ANSWER_FIELD_TOKENS):
            return False

        if not value.strip():
            return False
        if self._is_placeholder_value(value):
            return False

        # Skip masked/secret-like content.
        if re.fullmatch(r"[\*\u2022\s]{4,}", value.strip()):
            return False
        return True

    def learn_from_observations(self, observations: list[dict[str, Any]]) -> int:
        learned = 0
        changed = False

        for observation in observations:
            label = str(observation.get("label", "")).strip()
            context = str(observation.get("context", "")).strip()
            raw_value = str(observation.get("value", "")).strip()
            value = re.sub(r"\s+", " ", raw_value).strip()

            if len(value) > 420:
                value = value[:420].rstrip()
            if not self._is_learnable_observation(label, value):
                continue

            normalized_label = _normalize(label)
            if not normalized_label:
                continue

            previous = self.field_answers.get(normalized_label, "").strip()
            if previous != value:
                self.field_answers[normalized_label] = value
                learned += 1
                changed = True

            if context:
                compact_context = re.sub(r"\s+", " ", context).strip()[:220]
                combined_key = _normalize(f"{label} | {compact_context}")
                if combined_key:
                    previous_combined = self.field_answers.get(combined_key, "").strip()
                    if previous_combined != value:
                        self.field_answers[combined_key] = value
                        changed = True

            mapped_profile_key = self._match_profile_field(normalized_label)
            if mapped_profile_key and not self.profile.get(mapped_profile_key, "").strip():
                self.profile[mapped_profile_key] = value
                changed = True

        if changed:
            self.save()
        return learned

    def remember_copilot_recipe(
        self,
        state_signature: str,
        action_label: str,
        source: str = "human",
    ) -> None:
        signature = state_signature.strip()
        label = re.sub(r"\s+", " ", action_label).strip()
        if not signature or not label:
            return

        normalized_label = _normalize(label)
        if not normalized_label:
            return

        updated = False
        now_utc = datetime.utcnow().isoformat(timespec="seconds")
        for recipe in self.copilot_recipes:
            if not isinstance(recipe, dict):
                continue
            if recipe.get("state_signature") != signature:
                continue
            if recipe.get("action_label_normalized") != normalized_label:
                continue
            recipe["success_count"] = int(recipe.get("success_count", 0)) + 1
            recipe["updated_at_utc"] = now_utc
            recipe["source"] = source
            updated = True
            break

        if not updated:
            self.copilot_recipes.append(
                {
                    "state_signature": signature,
                    "action_label": label[:200],
                    "action_label_normalized": normalized_label,
                    "source": source,
                    "success_count": 1,
                    "updated_at_utc": now_utc,
                }
            )

        if len(self.copilot_recipes) > 500:
            self.copilot_recipes.sort(key=lambda item: int(item.get("success_count", 0)), reverse=True)
            del self.copilot_recipes[500:]
        self.save()

    def find_copilot_recipe_action(
        self,
        state_signature: str,
        candidates: list[dict[str, str]],
    ) -> tuple[int | None, str]:
        signature = state_signature.strip()
        if not signature or not candidates:
            return None, ""

        ranked_recipes = [
            recipe
            for recipe in self.copilot_recipes
            if isinstance(recipe, dict) and recipe.get("state_signature") == signature
        ]
        ranked_recipes.sort(key=lambda item: int(item.get("success_count", 0)), reverse=True)

        if not ranked_recipes:
            return None, ""

        candidate_labels = [
            _normalize(str(candidate.get("label", "")))
            for candidate in candidates
        ]

        for recipe in ranked_recipes:
            recipe_label = _normalize(str(recipe.get("action_label_normalized", "")))
            if not recipe_label:
                continue
            for index, candidate_label in enumerate(candidate_labels):
                if not candidate_label:
                    continue
                if recipe_label == candidate_label:
                    return index, "copilot_memory_exact"
                if recipe_label in candidate_label or candidate_label in recipe_label:
                    return index, "copilot_memory_partial"
        return None, ""
