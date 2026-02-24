from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from src.config import Settings, load_settings
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
    parser.add_argument("--no-external", action="store_true", help="Disable external forms for this run")
    parser.add_argument("--no-auto-submit", action="store_true", help="Ask before each submit")
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

    while True:
        user_value = input("Provide absolute path to your CV PDF: ").strip()
        candidate = Path(user_value).expanduser()
        if candidate.exists():
            return candidate
        print("File not found. Try again.")


def bootstrap_profile(knowledge: KnowledgeStore) -> None:
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
    knowledge.ensure_profile_fields(required_profile_fields)


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    updated = settings
    if args.max_jobs is not None:
        updated = replace(updated, max_jobs_per_run=max(1, args.max_jobs))
    if args.min_fit is not None:
        updated = replace(updated, min_fit_score=max(0, min(100, args.min_fit)))
    if args.dry_run:
        updated = replace(updated, dry_run=True)
    if args.no_external:
        updated = replace(updated, apply_external_forms=False)
    if args.no_auto_submit:
        updated = replace(updated, auto_submit=False)
    return updated


def main() -> None:
    args = parse_args()
    settings = apply_cli_overrides(load_settings(), args)

    cv_path = ensure_cv_path(settings, args.cv_path)
    cv_text = extract_cv_text(cv_path)

    knowledge = KnowledgeStore(settings.knowledge_path)
    bootstrap_profile(knowledge)

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
