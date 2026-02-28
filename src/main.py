from __future__ import annotations

import argparse
import json
import re
from dataclasses import replace
from pathlib import Path

from src.config import Settings, detect_system_browser_profile_dir, load_settings
from src.cv_tools import extract_cv_text
from src.knowledge_store import KnowledgeStore
from src.linkedin_bot import LinkedInJobApplier
from src.llm_agent import LLMJobAgent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LinkedIn Job Applier (Playwright Stealth)")
    parser.add_argument("--max-jobs", type=int, default=None, help="Override MAX_JOBS_PER_RUN")
    parser.add_argument("--min-fit", type=int, default=None, help="Override MIN_FIT_SCORE")
    parser.add_argument("--cv-path", type=str, default=None, help="Override CV_PATH")
    parser.add_argument("--dry-run", action="store_true", help="Analyze and fill forms but do not submit")
    parser.add_argument("--reapply", action="store_true", help="Ignore previous application history and process jobs again")
    parser.add_argument("--no-external", action="store_true", help="Disable external forms for this run")
    parser.add_argument("--no-auto-submit", action="store_true", help="Ask before each submit")
    parser.add_argument("--system-chrome", action="store_true", help="Use your installed Chrome profile/session")
    parser.add_argument("--chrome-user-data-dir", type=str, default=None, help="Chrome user data dir (for --system-chrome)")
    parser.add_argument("--chrome-profile", type=str, default=None, help="Chrome profile name, e.g. Default/Profile 1")
    parser.add_argument("--browser-channel", type=str, default=None, help="Playwright browser channel, e.g. chrome/msedge")
    return parser.parse_args()


def ensure_cv_path(settings: Settings, cli_cv_path: str | None) -> Path:
    if cli_cv_path:
        candidate = Path(cli_cv_path).expanduser()
        if not candidate.is_absolute():
            candidate = (settings.base_dir / candidate).resolve()
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"--cv-path does not exist: {candidate}")

    if settings.cv_path and settings.cv_path.exists():
        return settings.cv_path

    if not settings.terminal_input_enabled:
        raise FileNotFoundError("CV path is missing. Set CV_PATH in .env or pass --cv-path.")

    while True:
        user_value = input("Provide absolute path to your CV PDF: ").strip()
        candidate = Path(user_value).expanduser()
        if candidate.exists():
            return candidate
        print("File not found. Try again.")


def _normalize_bootstrap_section(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}

    parsed: dict[str, str] = {}
    for key, value in raw.items():
        key_str = str(key).strip()
        value_str = str(value).strip()
        if key_str and value_str:
            parsed[key_str] = value_str
    return parsed


def _normalize_field_key(value: str) -> str:
    normalized = value.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9 ]+", "", normalized)
    return normalized


def _load_profile_bootstrap(path: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    if path is None or not path.exists():
        return {}, {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        print(f"Profile bootstrap file is not valid JSON: {path}")
        return {}, {}

    if not isinstance(raw, dict):
        print(f"Profile bootstrap file must contain a JSON object: {path}")
        return {}, {}

    if "profile" in raw or "field_answers" in raw:
        profile_data = _normalize_bootstrap_section(raw.get("profile", {}))
        field_answers_data = _normalize_bootstrap_section(raw.get("field_answers", {}))
        return profile_data, field_answers_data

    # Backward compatibility: flat object means profile fields only.
    return _normalize_bootstrap_section(raw), {}


def bootstrap_profile(settings: Settings, knowledge: KnowledgeStore) -> None:
    bootstrap_profile_data, bootstrap_field_answers = _load_profile_bootstrap(settings.profile_bootstrap_path)
    updated = False
    for key, value in bootstrap_profile_data.items():
        if not knowledge.profile.get(key):
            knowledge.profile[key] = value
            updated = True

    for key, value in bootstrap_field_answers.items():
        normalized_key = _normalize_field_key(key)
        if not normalized_key:
            continue
        if not knowledge.field_answers.get(normalized_key):
            knowledge.field_answers[normalized_key] = value
            updated = True

    if updated:
        knowledge.save()

    required_profile_fields = {
        "full_name": "Your full name",
        "email": "Primary email",
        "phone": "Phone number with country code",
        "city": "Current city",
        "country": "Current country",
        "linkedin_url": "LinkedIn profile URL",
        "years_of_experience": "Total years of experience",
        "work_authorization": "Work authorization / visa sponsorship status",
    }
    if settings.profile_prompt_on_start:
        knowledge.ensure_profile_fields(required_profile_fields)


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    updated = settings
    if args.max_jobs is not None:
        updated = replace(updated, max_jobs_per_run=max(1, args.max_jobs))
    if args.min_fit is not None:
        updated = replace(updated, min_fit_score=max(0, min(100, args.min_fit)))
    if args.dry_run:
        updated = replace(updated, dry_run=True)
    if args.reapply:
        updated = replace(updated, skip_already_applied=False)
    if args.no_external:
        updated = replace(updated, apply_external_forms=False)
    if args.no_auto_submit:
        updated = replace(updated, auto_submit=False)
    if args.system_chrome:
        updated = replace(updated, use_system_chrome_profile=True)
        if updated.system_chrome_user_data_dir is None:
            detected_dir, detected_channel = detect_system_browser_profile_dir()
            updated = replace(updated, system_chrome_user_data_dir=detected_dir)
            if not updated.browser_channel and detected_channel:
                updated = replace(updated, browser_channel=detected_channel)
    if args.chrome_user_data_dir:
        candidate = Path(args.chrome_user_data_dir).expanduser()
        if not candidate.is_absolute():
            candidate = (settings.base_dir / candidate).resolve()
        updated = replace(updated, system_chrome_user_data_dir=candidate)
    if args.chrome_profile:
        updated = replace(updated, system_chrome_profile_name=args.chrome_profile.strip())
    if args.browser_channel:
        updated = replace(updated, browser_channel=args.browser_channel.strip().lower() or None)
    return updated


def main() -> None:
    args = parse_args()
    settings = apply_cli_overrides(load_settings(), args)

    cv_path = ensure_cv_path(settings, args.cv_path)
    cv_text = extract_cv_text(cv_path)

    knowledge = KnowledgeStore(
        settings.knowledge_path,
        interactive_prompts=settings.terminal_input_enabled,
    )
    bootstrap_profile(settings, knowledge)

    if not settings.openai_api_key:
        print("OPENAI_API_KEY is empty. Running with heuristic scoring only.")

    llm = LLMJobAgent(api_key=settings.openai_api_key, model=settings.openai_model)
    bot = LinkedInJobApplier(
        settings=settings,
        knowledge=knowledge,
        llm=llm,
        cv_text=cv_text,
        cv_path=cv_path,
    )
    bot.run()


if __name__ == "__main__":
    main()
