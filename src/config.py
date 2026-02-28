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


def detect_system_browser_profile_dir() -> tuple[Path | None, str | None]:
    candidates: list[tuple[Path, str]] = [
        (Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data", "chrome"),
        (Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data", "msedge"),
        (Path.home() / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data", "chrome"),
    ]
    for candidate_path, channel in candidates:
        if candidate_path.exists():
            return candidate_path, channel
    return None, None


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
    use_system_chrome_profile: bool
    system_chrome_user_data_dir: Path | None
    system_chrome_profile_name: str
    browser_channel: str | None
    profile_bootstrap_path: Path | None
    profile_prompt_on_start: bool
    always_apply_except_outside_poland: bool
    copilot_mode: bool
    terminal_input_enabled: bool
    copilot_wait_timeout_sec: int
    copilot_poll_interval_ms: int
    copilot_auto_skip_on_timeout: bool


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parents[1]
    load_dotenv(base_dir / ".env")

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

    cv_path = _to_path(base_dir, os.getenv("CV_PATH", "").strip())
    knowledge_path = _to_path(base_dir, os.getenv("KNOWLEDGE_PATH", "data/knowledge.json"))
    browser_profile_dir = _to_path(base_dir, os.getenv("BROWSER_PROFILE_DIR", "data/browser-profile"))
    system_chrome_user_data_dir = _to_path(base_dir, os.getenv("SYSTEM_CHROME_USER_DATA_DIR", ""))
    profile_bootstrap_path = _to_path(base_dir, os.getenv("PROFILE_BOOTSTRAP_PATH", "data/profile.bootstrap.json"))

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
    use_system_chrome_profile = _to_bool(os.getenv("USE_SYSTEM_CHROME_PROFILE"), default=False)
    system_chrome_profile_name = os.getenv("SYSTEM_CHROME_PROFILE_NAME", "Default").strip() or "Default"
    browser_channel_raw = os.getenv("BROWSER_CHANNEL", "").strip().lower()
    browser_channel = browser_channel_raw or None
    profile_prompt_on_start = _to_bool(os.getenv("PROFILE_PROMPT_ON_START"), default=False)
    always_apply_except_outside_poland = _to_bool(os.getenv("ALWAYS_APPLY_EXCEPT_OUTSIDE_POLAND"), default=True)
    copilot_mode = _to_bool(os.getenv("COPILOT_MODE"), default=True)
    terminal_input_enabled = _to_bool(os.getenv("TERMINAL_INPUT_ENABLED"), default=False)
    copilot_wait_timeout_sec = max(10, int(os.getenv("COPILOT_WAIT_TIMEOUT_SEC", "240")))
    copilot_poll_interval_ms = max(200, int(os.getenv("COPILOT_POLL_INTERVAL_MS", "900")))
    copilot_auto_skip_on_timeout = _to_bool(os.getenv("COPILOT_AUTO_SKIP_ON_TIMEOUT"), default=True)

    if use_system_chrome_profile and system_chrome_user_data_dir is None:
        detected_dir, detected_channel = detect_system_browser_profile_dir()
        system_chrome_user_data_dir = detected_dir
        if not browser_channel and detected_channel:
            browser_channel = detected_channel
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
        use_system_chrome_profile=use_system_chrome_profile,
        system_chrome_user_data_dir=system_chrome_user_data_dir,
        system_chrome_profile_name=system_chrome_profile_name,
        browser_channel=browser_channel,
        profile_bootstrap_path=profile_bootstrap_path,
        profile_prompt_on_start=profile_prompt_on_start,
        always_apply_except_outside_poland=always_apply_except_outside_poland,
        copilot_mode=copilot_mode,
        terminal_input_enabled=terminal_input_enabled,
        copilot_wait_timeout_sec=copilot_wait_timeout_sec,
        copilot_poll_interval_ms=copilot_poll_interval_ms,
        copilot_auto_skip_on_timeout=copilot_auto_skip_on_timeout,
    )
