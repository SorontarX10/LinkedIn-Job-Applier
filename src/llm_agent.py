from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from src.models import FitDecision, JobPosting


class LLMJobAgent:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.enabled = bool(api_key.strip())
        self.client = OpenAI(api_key=api_key) if self.enabled else None

    def analyze_job(
        self,
        job: JobPosting,
        cv_text: str,
        profile: dict[str, str],
        known_answers: dict[str, str],
    ) -> FitDecision:
        if not self.enabled:
            return self._heuristic_decision(job)

        system_prompt = (
            "You are a strict career assistant. You must never invent facts.\n"
            "Use only information from CV, profile and known answers.\n"
            "If data is missing, list what is missing instead of guessing.\n"
            "Return valid JSON only."
        )

        user_payload = {
            "job": {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "description": job.description[:15000],
                "apply_mode": job.apply_mode,
                "url": job.url,
            },
            "candidate_context": {
                "cv_text": cv_text[:15000],
                "profile": profile,
                "known_answers": known_answers,
            },
            "output_format": {
                "should_apply": "boolean",
                "fit_score": "integer 0-100",
                "reasoning": "short string",
                "missing_information": ["list of factual missing questions"],
                "tailored_cv_notes": ["list of truthful CV tailoring notes"],
                "cover_letter": "string, empty if not needed",
                "prefilled_answers": {"field label": "answer from known facts only"},
            },
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.2,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)
            return FitDecision(
                should_apply=bool(data.get("should_apply", False)),
                fit_score=self._safe_int(data.get("fit_score", 0)),
                reasoning=str(data.get("reasoning", "")).strip(),
                missing_information=self._safe_list(data.get("missing_information")),
                tailored_cv_notes=self._safe_list(data.get("tailored_cv_notes")),
                cover_letter=str(data.get("cover_letter", "")).strip(),
                prefilled_answers=self._safe_dict(data.get("prefilled_answers")),
            )
        except Exception as exc:
            fallback = self._heuristic_decision(job)
            fallback.reasoning = f"{fallback.reasoning} LLM error: {exc}"
            return fallback

    @staticmethod
    def _safe_json(raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    @staticmethod
    def _safe_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _safe_dict(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        parsed: dict[str, str] = {}
        for key, item in value.items():
            key_str = str(key).strip()
            item_str = str(item).strip()
            if key_str and item_str and item_str.lower() not in {"null", "none", "unknown"}:
                parsed[key_str] = item_str
        return parsed

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, parsed))

    @staticmethod
    def _heuristic_decision(job: JobPosting) -> FitDecision:
        title = job.title.lower()
        positive_tokens = ("python", "automation", "backend", "engineer", "developer")
        score = 35 + sum(12 for token in positive_tokens if token in title)
        score = max(0, min(100, score))
        return FitDecision(
            should_apply=score >= 50,
            fit_score=score,
            reasoning="Fallback heuristic used (OPENAI_API_KEY missing).",
        )
