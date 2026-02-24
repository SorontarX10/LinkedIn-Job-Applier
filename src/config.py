from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _to_bool(raw_value: str | None, default: bool = False) -> bool:
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _to_path(base_dir: Path, raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    parsed = Path(raw_path).expanduser()
    if parsed.is_absolute():
        return parsed
    return (base_dir / parsed).resolve()


@dataclass
class Settings:
    base_dir: Path
    openai_api_key: str
    openai_model: str
    cv_path: Path | None
    knowledge_path: Path
    browser_profile_dir: Path
    max_jobs_per_run: int
    min_fit_score: int
    auto_submit: bool
    apply_external_forms: bool
    dry_run: bool
    skip_already_applied: bool
    slow_mo_ms: int
    ai_disclosure_enabled: bool
    ai_disclosure_text: str


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parents[1]
    load_dotenv(base_dir / ".env")

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

    cv_path = _to_path(base_dir, os.getenv("CV_PATH", "").strip())
    knowledge_path = _to_path(base_dir, os.getenv("KNOWLEDGE_PATH", "data/knowledge.json"))
    browser_profile_dir = _to_path(base_dir, os.getenv("BROWSER_PROFILE_DIR", "data/browser-profile"))

    if knowledge_path is None or browser_profile_dir is None:
        raise ValueError("KNOWLEDGE_PATH and BROWSER_PROFILE_DIR must resolve to valid paths.")

    max_jobs_per_run = int(os.getenv("MAX_JOBS_PER_RUN", "10"))
    min_fit_score = int(os.getenv("MIN_FIT_SCORE", "50"))
    slow_mo_ms = int(os.getenv("SLOW_MO_MS", "150"))
    ai_disclosure_enabled = _to_bool(os.getenv("AI_DISCLOSURE_ENABLED"), default=True)
    ai_disclosure_text = os.getenv(
        "AI_DISCLOSURE_TEXT",
        "This application was submitted with assistance from an AI agent.",
    ).strip()
    if not ai_disclosure_text:
        ai_disclosure_text = "This application was submitted with assistance from an AI agent."

    return Settings(
        base_dir=base_dir,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        cv_path=cv_path,
        knowledge_path=knowledge_path,
        browser_profile_dir=browser_profile_dir,
        max_jobs_per_run=max_jobs_per_run,
        min_fit_score=min_fit_score,
        auto_submit=_to_bool(os.getenv("AUTO_SUBMIT"), default=True),
        apply_external_forms=_to_bool(os.getenv("APPLY_EXTERNAL_FORMS"), default=True),
        dry_run=_to_bool(os.getenv("DRY_RUN"), default=False),
        skip_already_applied=_to_bool(os.getenv("SKIP_ALREADY_APPLIED"), default=True),
        slow_mo_ms=slow_mo_ms,
        ai_disclosure_enabled=ai_disclosure_enabled,
        ai_disclosure_text=ai_disclosure_text,
    )
