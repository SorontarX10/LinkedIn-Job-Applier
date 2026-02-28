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


def _to_list(raw_value: str | None) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    value = raw_value.strip()
    if not value:
        return ()
    normalized = value.replace("\n", ",").replace(";", ",")
    parts = [item.strip() for item in normalized.split(",")]
    return tuple(item for item in parts if item)


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
    job_queue_path: Path
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
    agentic_fallback_max_iterations: int
    agentic_tool_step_limit: int
    agentic_tool_timeout_sec: int
    agentic_blocked_action_tokens: tuple[str, ...]
    agentic_playbook_confidence_threshold: float
    agentic_playbook_min_uses: int
    discovery_enabled: bool
    discovery_keywords_include: tuple[str, ...]
    discovery_keywords_exclude: tuple[str, ...]
    discovery_locations: tuple[str, ...]
    discovery_remote_only: bool
    discovery_days_back: int
    discovery_max_results: int
    discovery_cache_path: Path
    discovery_cache_ttl_minutes: int
    job_queue_retry_limit: int
    job_queue_retry_cooldown_minutes: int


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parents[1]
    load_dotenv(base_dir / ".env")

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

    cv_path = _to_path(base_dir, os.getenv("CV_PATH", "").strip())
    knowledge_path = _to_path(base_dir, os.getenv("KNOWLEDGE_PATH", "data/knowledge.json"))
    job_queue_path = _to_path(base_dir, os.getenv("JOB_QUEUE_PATH", "data/job_queue.jsonl"))
    browser_profile_dir = _to_path(base_dir, os.getenv("BROWSER_PROFILE_DIR", "data/browser-profile"))
    system_chrome_user_data_dir = _to_path(base_dir, os.getenv("SYSTEM_CHROME_USER_DATA_DIR", ""))
    profile_bootstrap_path = _to_path(base_dir, os.getenv("PROFILE_BOOTSTRAP_PATH", "data/profile.bootstrap.json"))

    if knowledge_path is None or job_queue_path is None or browser_profile_dir is None:
        raise ValueError("KNOWLEDGE_PATH, JOB_QUEUE_PATH and BROWSER_PROFILE_DIR must resolve to valid paths.")

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
    agentic_fallback_max_iterations = max(1, int(os.getenv("AGENTIC_FALLBACK_MAX_ITERATIONS", "4")))
    agentic_tool_step_limit = max(4, int(os.getenv("AGENTIC_TOOL_STEP_LIMIT", "32")))
    agentic_tool_timeout_sec = max(20, int(os.getenv("AGENTIC_TOOL_TIMEOUT_SEC", "120")))
    blocked_raw = os.getenv(
        "AGENTIC_BLOCKED_ACTION_TOKENS",
        "discard,close application,withdraw,delete,remove,logout,log out,sign out,cancel application",
    ).strip()
    agentic_blocked_action_tokens = tuple(
        token.strip() for token in blocked_raw.split(",") if token.strip()
    )
    try:
        agentic_playbook_confidence_threshold = float(os.getenv("AGENTIC_PLAYBOOK_CONFIDENCE_THRESHOLD", "0.60"))
    except ValueError:
        agentic_playbook_confidence_threshold = 0.60
    agentic_playbook_confidence_threshold = max(0.0, min(1.0, agentic_playbook_confidence_threshold))
    agentic_playbook_min_uses = max(1, int(os.getenv("AGENTIC_PLAYBOOK_MIN_USES", "1")))
    discovery_enabled = _to_bool(os.getenv("DISCOVERY_ENABLED"), default=False)
    discovery_keywords_include = _to_list(os.getenv("DISCOVERY_KEYWORDS_INCLUDE", ""))
    discovery_keywords_exclude = _to_list(os.getenv("DISCOVERY_KEYWORDS_EXCLUDE", ""))
    discovery_locations = _to_list(os.getenv("DISCOVERY_LOCATIONS", ""))
    discovery_remote_only = _to_bool(os.getenv("DISCOVERY_REMOTE_ONLY"), default=False)
    discovery_days_back = max(1, int(os.getenv("DISCOVERY_DAYS_BACK", "30")))
    discovery_max_results = max(10, int(os.getenv("DISCOVERY_MAX_RESULTS", "60")))
    discovery_cache_path = _to_path(base_dir, os.getenv("DISCOVERY_CACHE_PATH", "data/job_discovery_cache.json"))
    if discovery_cache_path is None:
        raise ValueError("DISCOVERY_CACHE_PATH must resolve to a valid path.")
    discovery_cache_ttl_minutes = max(1, int(os.getenv("DISCOVERY_CACHE_TTL_MINUTES", "90")))
    job_queue_retry_limit = max(1, int(os.getenv("JOB_QUEUE_RETRY_LIMIT", "3")))
    job_queue_retry_cooldown_minutes = max(0, int(os.getenv("JOB_QUEUE_RETRY_COOLDOWN_MINUTES", "30")))

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
        job_queue_path=job_queue_path,
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
        agentic_fallback_max_iterations=agentic_fallback_max_iterations,
        agentic_tool_step_limit=agentic_tool_step_limit,
        agentic_tool_timeout_sec=agentic_tool_timeout_sec,
        agentic_blocked_action_tokens=agentic_blocked_action_tokens,
        agentic_playbook_confidence_threshold=agentic_playbook_confidence_threshold,
        agentic_playbook_min_uses=agentic_playbook_min_uses,
        discovery_enabled=discovery_enabled,
        discovery_keywords_include=discovery_keywords_include,
        discovery_keywords_exclude=discovery_keywords_exclude,
        discovery_locations=discovery_locations,
        discovery_remote_only=discovery_remote_only,
        discovery_days_back=discovery_days_back,
        discovery_max_results=discovery_max_results,
        discovery_cache_path=discovery_cache_path,
        discovery_cache_ttl_minutes=discovery_cache_ttl_minutes,
        job_queue_retry_limit=job_queue_retry_limit,
        job_queue_retry_cooldown_minutes=job_queue_retry_cooldown_minutes,
    )
