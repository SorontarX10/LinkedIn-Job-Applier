from __future__ import annotations

import json
import hashlib
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright
from playwright_stealth import Stealth

from src.config import Settings
from src.cv_tools import write_cover_letter, write_tailored_cv_notes
from src.agentic_fallback import AgenticFallbackController
from src.form_helper import FormHelper
from src.job_discovery import JobDiscovery
from src.job_queue import JobQueueStore
from src.knowledge_store import KnowledgeStore
from src.llm_agent import LLMJobAgent
from src.models import ApplicationRecord, FitDecision, JobPosting
from src.run_metrics import RunMetricsTracker


def _normalize(text: str) -> str:
    normalized = text.translate(
        str.maketrans(
            "ąćęłńóśżźĄĆĘŁŃÓŚŻŹ",
            "acelnoszzACELNOSZZ",
        )
    )
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = normalized.encode("ascii", "ignore").decode("ascii", "ignore")
    lowered = normalized.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", "", lowered)
    return lowered


class LinkedInJobApplier:
    SAVED_JOBS_URL = "https://www.linkedin.com/my-items/saved-jobs/?cardType=SAVED"
    FEED_URL = "https://www.linkedin.com/feed/"

    EASY_APPLY_TOKENS = ("easy apply", "latwe aplikowanie")
    EXTERNAL_APPLY_TOKENS = ("apply", "aplikuj", "visit", "apply on company")
    NEXT_TOKENS = ("next", "dalej", "continue", "kontynuuj")
    REVIEW_TOKENS = ("review", "przejrzyj")
    SUBMIT_TOKENS = ("submit application", "wyslij aplikacje", "submit", "aplikuj")
    CLOSE_TOKENS = ("dismiss", "close", "cancel", "anuluj", "zamknij")
    DISCARD_TOKENS = ("discard", "odrzuc", "leave", "wyjdz")
    UNSAVE_TOKENS = ("saved", "unsave", "remove from saved", "zapisane", "usun z zapisanych")
    EXTERNAL_PROGRESS_TOKENS = (
        "apply now",
        "start application",
        "continue application",
        "continue",
        "next",
        "dalej",
        "review",
        "proceed",
        "apply",
        "send",
        "finish",
        "complete",
    )
    EXTERNAL_EXCLUDE_TOKENS = (
        "sign in",
        "login",
        "log in",
        "create account",
        "register",
        "privacy",
        "terms",
        "about",
        "help",
        "home",
        "back",
        "cancel",
        "anuluj",
    )
    def __init__(
        self,
        settings: Settings,
        knowledge: KnowledgeStore,
        llm: LLMJobAgent,
        cv_text: str,
        cv_path: Path,
        run_mode: str = "saved_only",
        discover_max: int | None = None,
    ):
        self.settings = settings
        self.knowledge = knowledge
        self.llm = llm
        self.cv_text = cv_text
        self.cv_path = cv_path
        self.stealth = Stealth()
        self.agentic_fallback = AgenticFallbackController(
            llm=self.llm,
            knowledge=self.knowledge,
            base_dir=self.settings.base_dir,
            max_iterations=self.settings.agentic_fallback_max_iterations,
            tool_step_limit=self.settings.agentic_tool_step_limit,
            tool_timeout_sec=self.settings.agentic_tool_timeout_sec,
            blocked_action_tokens=self.settings.agentic_blocked_action_tokens,
            playbook_confidence_threshold=self.settings.agentic_playbook_confidence_threshold,
            playbook_min_uses=self.settings.agentic_playbook_min_uses,
            llm_plan_enabled=self.settings.agentic_llm_plan_enabled,
            llm_plan_max_steps=self.settings.agentic_llm_plan_max_steps,
        )
        self.job_queue = JobQueueStore(
            self.settings.job_queue_path,
            retry_limit=self.settings.job_queue_retry_limit,
            retry_cooldown_minutes=self.settings.job_queue_retry_cooldown_minutes,
        )
        self.discovery = JobDiscovery(
            cache_path=self.settings.discovery_cache_path,
            cache_ttl_minutes=self.settings.discovery_cache_ttl_minutes,
        )
        self.run_mode = run_mode.strip().lower() if run_mode else "saved_only"
        self.discover_max = max(1, int(discover_max)) if discover_max is not None else self.settings.discovery_max_results
        self.metrics = RunMetricsTracker(base_dir=self.settings.base_dir, mode=self.run_mode)
        self.last_apply_note = ""

    def run(self) -> None:
        allowed_modes = {"saved_only", "discovery_only", "discovery_and_apply"}
        mode = self.run_mode if self.run_mode in allowed_modes else "saved_only"
        if mode != self.run_mode:
            print(f"Unknown run mode '{self.run_mode}'. Falling back to 'saved_only'.")

        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if not self._ensure_logged_in(page):
                    print("Cannot continue without active LinkedIn session.")
                    return

                if mode in {"saved_only", "discovery_and_apply"}:
                    saved_urls = self._sync_saved_jobs_into_queue(page)
                    if mode == "saved_only" and not saved_urls:
                        return

                discovery_enabled_for_run = self.settings.discovery_enabled
                if mode in {"discovery_only", "discovery_and_apply"}:
                    if not discovery_enabled_for_run:
                        print("Discovery is disabled (`DISCOVERY_ENABLED=false`).")
                        if mode == "discovery_only":
                            return
                    else:
                        effective_discover_max = min(self.discover_max, self.settings.discovery_max_results)
                        summary = self._run_discovery_pipeline(page, max_results=effective_discover_max)
                        self.metrics.record_discovery_summary(summary)
                        print(
                            "Discovery summary: "
                            f"queries={summary['queries']}, discovered={summary['discovered']}, "
                            f"queued={summary['queued']}, rejected={summary['rejected']}"
                        )
                    if mode == "discovery_only":
                        print("Discovery-only mode completed (no apply phase).")
                        return

                queue_sources = {"saved"}
                if mode != "saved_only" and discovery_enabled_for_run:
                    queue_sources.add("discovery")
                queue_urls = self.job_queue.get_top_queued_urls(
                    limit=self.settings.max_jobs_per_run,
                    sources=queue_sources,
                )
                if not queue_urls:
                    print("No queued jobs to process for current mode.")
                    return

                self.metrics.set_queue_context(
                    selected_count=len(queue_urls),
                    sources=queue_sources,
                )
                print(f"Queued jobs selected: {len(queue_urls)}")
                processed = 0
                for job_url in queue_urls:
                    processed += 1
                    print(f"\n[{processed}] Processing: {job_url}")
                    queued_record = self.job_queue.get_record(job_url) or {}
                    queue_source = str(queued_record.get("source", "")).strip().lower() or "unknown"
                    started_monotonic = time.perf_counter()
                    self.metrics.start_job(
                        job_url=job_url,
                        source=queue_source,
                        started_monotonic=started_monotonic,
                    )
                    self.job_queue.mark_in_progress(job_url)
                    context, page = self._recover_page_or_context(playwright, context, page)
                    if context is None or page is None:
                        print("Session recovery failed before processing. Stopping.")
                        self._record_metrics_job_completion(job_url=job_url, ended_monotonic=time.perf_counter())
                        return

                    attempt = 0
                    while attempt < 2:
                        attempt += 1
                        try:
                            self._process_single_job(page, context, job_url)
                            self._sync_job_queue_with_application_record(job_url)
                            self._record_metrics_job_completion(job_url=job_url, ended_monotonic=time.perf_counter())
                            break
                        except Exception as exc:
                            if self._is_target_closed_exception(exc):
                                print("Detected closed target/page during processing. Attempting recovery...")
                                recovered_context, recovered_page = self._recover_page_or_context(playwright, context, page)
                                if recovered_context is None or recovered_page is None:
                                    print("TargetClosed recovery failed. Stopping.")
                                    return
                                context = recovered_context
                                page = recovered_page
                                if attempt < 2:
                                    print("Recovered browser state. Retrying current job once.")
                                    continue

                            error_name = type(exc).__name__
                            error_text = " ".join(str(exc).split())
                            if len(error_text) > 180:
                                error_text = f"{error_text[:177]}..."
                            print(f"Error while processing {job_url}: {error_name}: {error_text}")
                            self.knowledge.save_application(
                                ApplicationRecord(
                                    url=job_url,
                                    title="Unknown title",
                                    company="Unknown company",
                                    status="error",
                                    notes=f"{error_name}: {error_text}",
                                )
                            )
                            self._sync_job_queue_with_application_record(job_url)
                            self._record_metrics_job_completion(job_url=job_url, ended_monotonic=time.perf_counter())
                            context, page = self._recover_page_or_context(playwright, context, page)
                            if context is None or page is None:
                                print("Session recovery failed after error. Stopping.")
                                return
                            break
            finally:
                report_path = self.metrics.finalize_and_save()
                if report_path:
                    print(f"Metrics report: {report_path}")
                try:
                    context.close()
                except Exception:
                    pass

    def _sync_saved_jobs_into_queue(self, page: Page) -> list[str]:
        job_urls = self._get_saved_job_urls(page)
        if not job_urls:
            print("No saved jobs found.")
            return []

        queued_count = self.job_queue.enqueue_saved_jobs(job_urls)
        if queued_count:
            print(f"Job queue synchronized from saved jobs: {queued_count} entries updated.")

        print(f"Saved jobs detected: {len(job_urls)}")
        return job_urls

    def _run_discovery_pipeline(self, page: Page, max_results: int) -> dict[str, int]:
        configured_locations = [item.strip() for item in self.settings.discovery_locations if item.strip()]
        locations: list[str] = configured_locations
        if not locations:
            city = str(self.knowledge.profile.get("city", "")).strip()
            country = str(self.knowledge.profile.get("country", "")).strip()
            if city and country:
                locations.append(f"{city}, {country}")
            elif country:
                locations.append(country)

        effective_max_results = max(10, min(max_results, self.settings.discovery_max_results))
        query_limit = max(4, min(15, (effective_max_results // 12) + 3))
        queries = self.discovery.build_queries(
            profile=self.knowledge.profile,
            cv_text=self.cv_text,
            include_keywords=list(self.settings.discovery_keywords_include),
            exclude_keywords=list(self.settings.discovery_keywords_exclude),
            locations=locations or None,
            remote_only=self.settings.discovery_remote_only,
            days_back=self.settings.discovery_days_back,
            max_queries=query_limit,
        )
        if not queries:
            return {"queries": 0, "discovered": 0, "queued": 0, "rejected": 0}

        discovered_jobs = self.discovery.discover_jobs(
            page=page,
            queries=queries,
            max_results=effective_max_results,
            pages_per_query=2,
            scroll_iterations=3,
            scroll_px=2600,
        )
        if not discovered_jobs:
            return {"queries": len(queries), "discovered": 0, "queued": 0, "rejected": 0}

        ranked = self.llm.rank_discovery_jobs(
            jobs=discovered_jobs,
            profile=self.knowledge.profile,
            known_answers=self.knowledge.field_answers,
            cv_text=self.cv_text,
            preferences={
                "prefer_poland": True,
                "hard_reject_outside_poland": False,
                "keywords_include": list(self.settings.discovery_keywords_include),
                "keywords_exclude": list(self.settings.discovery_keywords_exclude),
            },
        )

        payload: list[dict[str, Any]] = []
        rejected_count = 0
        for job, score in ranked:
            hard_reject = bool(getattr(score, "hard_reject", False))
            if hard_reject:
                rejected_count += 1
            payload.append(
                {
                    "job_id": str(getattr(job, "job_id", "")).strip(),
                    "url": str(getattr(job, "url", "")).strip(),
                    "title": str(getattr(job, "title", "")).strip(),
                    "company": str(getattr(job, "company", "")).strip(),
                    "location": str(getattr(job, "location", "")).strip(),
                    "score": int(getattr(score, "priority_score", 0)),
                    "hard_reject": hard_reject,
                    "reason": str(getattr(score, "reject_reason", "")).strip() or str(getattr(score, "reasoning", "")).strip(),
                    "source": "discovery",
                }
            )

        queued_count = self.job_queue.enqueue_discovery_jobs(payload)
        top_preview = payload[:5]
        if top_preview:
            print("Top discovery results:")
            for index, item in enumerate(top_preview, start=1):
                print(
                    f"  {index}. score={item['score']} | {item['title']} | {item['company']} | {item['location']}"
                )

        return {
            "queries": len(queries),
            "discovered": len(discovered_jobs),
            "queued": queued_count,
            "rejected": rejected_count,
        }

    def _recover_page_or_context(
        self,
        playwright: Playwright,
        context: BrowserContext | None,
        page: Page | None,
    ) -> tuple[BrowserContext | None, Page | None]:
        active_context = context
        active_page = page

        if active_context is None or self._is_context_closed(active_context):
            try:
                active_context = self._launch_context(playwright)
            except Exception:
                return None, None
            active_page = None

        if active_page is None or self._is_page_closed(active_page):
            try:
                active_page = active_context.pages[0] if active_context.pages else active_context.new_page()
            except Exception:
                return None, None

        try:
            if not self._ensure_logged_in(active_page):
                return None, None
        except Exception as exc:
            if self._is_target_closed_exception(exc):
                return None, None
            return None, None

        return active_context, active_page

    def _launch_context(self, playwright: Playwright) -> BrowserContext:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        launch_channel = self.settings.browser_channel
        user_data_dir = self.settings.browser_profile_dir

        if self.settings.use_system_chrome_profile:
            if self.settings.system_chrome_user_data_dir is None:
                raise RuntimeError(
                    "System Chrome profile mode is enabled, but SYSTEM_CHROME_USER_DATA_DIR is not configured."
                )
            user_data_dir = self.settings.system_chrome_user_data_dir
            if not user_data_dir.exists():
                raise RuntimeError(f"System browser profile directory does not exist: {user_data_dir}")
            launch_args.append(f"--profile-directory={self.settings.system_chrome_profile_name}")
            if not launch_channel:
                launch_channel = "chrome"
        else:
            self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": False,
            "slow_mo": self.settings.slow_mo_ms,
            "args": launch_args,
            "viewport": {"width": 1400, "height": 1000},
        }
        if launch_channel:
            launch_kwargs["channel"] = launch_channel

        try:
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            if self.settings.use_system_chrome_profile:
                raise RuntimeError(
                    "Could not open your system Chrome profile. Close all Chrome windows and retry, "
                    "or disable USE_SYSTEM_CHROME_PROFILE."
                ) from exc
            raise

        try:
            self.stealth.apply_stealth_sync(context)
        except Exception:
            pass
        return context

    def _ensure_logged_in(self, page: Page) -> bool:
        self._goto_with_retries(page, self.FEED_URL)
        page.wait_for_timeout(1200)

        if self._is_linkedin_authenticated(page):
            return True

        print("LinkedIn session not detected. Log in in the browser window; bot will resume automatically.")
        if self._requires_linkedin_login(page):
            try:
                self._goto_with_retries(page, "https://www.linkedin.com/login")
                page.wait_for_timeout(800)
            except Exception:
                pass

        deadline = time.time() + float(self.settings.copilot_wait_timeout_sec)
        while time.time() < deadline:
            if self._is_page_closed(page):
                return False
            if self._is_linkedin_authenticated(page):
                try:
                    self._goto_with_retries(page, self.FEED_URL)
                    page.wait_for_timeout(900)
                except Exception:
                    pass
                return self._is_linkedin_authenticated(page)
            page.wait_for_timeout(self.settings.copilot_poll_interval_ms)

        print("LinkedIn login wait timed out. Please log in and run again.")
        return False

    def _is_linkedin_authenticated(self, page: Page) -> bool:
        url = page.url.lower()
        if "linkedin.com" not in url:
            return False
        if self._requires_linkedin_login(page):
            return False

        selectors = (
            "input[placeholder*='Search' i][aria-label*='Search' i]",
            "a[href*='/mynetwork/']",
            "a[href*='/jobs/']",
            "button[aria-label*='Me' i]",
            "img.global-nav__me-photo",
            "div.global-nav",
        )
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _requires_linkedin_login(self, page: Page) -> bool:
        url = page.url.lower()
        if "linkedin.com" not in url:
            return False
        if any(token in url for token in ("/login", "/checkpoint", "/authwall", "/uas/login", "/signup", "/join")):
            return True

        selectors = (
            "input[name='session_key']",
            "input#username",
            "input[name='session_password']",
            "button[type='submit'][data-id='sign-in-form__submit-btn']",
            "a[href*='/login']",
            "a[href*='/signup']",
        )
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue

        try:
            page_text = _normalize(page.locator("body").inner_text()[:2200])
        except Exception:
            return False
        return any(token in page_text for token in ("sign in", "join now", "new to linkedin", "zaloguj", "dolacz"))

    def _get_saved_job_urls(self, page: Page) -> list[str]:
        self._goto_with_retries(page, self.SAVED_JOBS_URL)
        page.wait_for_timeout(1800)
        if self._requires_linkedin_login(page):
            print("Saved jobs page requires login.")
            return []
        # LinkedIn can occasionally drop query params during redirects.
        if "cardtype=saved" not in page.url.lower():
            self._goto_with_retries(page, self.SAVED_JOBS_URL)
            page.wait_for_timeout(1200)
            if self._requires_linkedin_login(page):
                print("Saved jobs page requires login.")
                return []
        self._scroll_down(page, iterations=8, pixels=3500)

        links = page.locator("a[href*='/jobs/view/']")
        urls: list[str] = []
        seen: set[str] = set()
        for index in range(links.count()):
            href = links.nth(index).get_attribute("href")
            if not href:
                continue
            absolute = urljoin("https://www.linkedin.com", href).split("?")[0].strip()
            if "/jobs/view/" not in absolute:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            urls.append(absolute)
        return urls

    def _scroll_down(self, page: Page, iterations: int, pixels: int) -> None:
        for _ in range(iterations):
            page.mouse.wheel(0, pixels)
            page.wait_for_timeout(700)

    def _sync_job_queue_with_application_record(self, job_url: str) -> None:
        record = self.knowledge.get_application_record(job_url)
        self.job_queue.sync_from_application_record(job_url, record)

    def _record_metrics_job_completion(self, *, job_url: str, ended_monotonic: float) -> None:
        record = self.knowledge.get_application_record(job_url)
        if not isinstance(record, dict):
            self.metrics.finish_job(
                job_url=job_url,
                status="unknown",
                notes="missing_application_record",
                ended_monotonic=ended_monotonic,
            )
            return
        status = str(record.get("status", "")).strip().lower() or "unknown"
        notes = str(record.get("notes", "")).strip()
        self.metrics.finish_job(
            job_url=job_url,
            status=status,
            notes=notes,
            ended_monotonic=ended_monotonic,
        )

    def _process_single_job(self, page: Page, context: BrowserContext, job_url: str) -> None:
        self.last_apply_note = ""
        if self.settings.skip_already_applied:
            record = self.knowledge.get_application_record(job_url)
            previous_status = str(record.get("status", "")).strip().lower() if isinstance(record, dict) else ""

            if previous_status == "submitted":
                if self._unsave_job(page, job_url):
                    print("Already applied job was removed from saved list.")
                print("Skipped: already submitted before.")
                return

            if previous_status == "not_submitted":
                print("Requeued: previous attempt was not_submitted, retrying now.")

            elif previous_status:
                print(f"Skipped: already processed before (status={previous_status}).")
                return

        job = self._read_job_posting(page, job_url)
        if not job.description.strip():
            print("Skipped: missing job description.")
            self.knowledge.save_application(
                ApplicationRecord(
                    url=job.url,
                    title=job.title,
                    company=job.company,
                    status="skipped_missing_description",
                    notes="missing_description",
                )
            )
            return
        if job.apply_mode == "unknown" and self._find_easy_apply_trigger(page):
            job.apply_mode = "easy"
            print("Apply mode corrected to: easy")
        print(f"Apply mode detected: {job.apply_mode}")

        decision = self.llm.analyze_job(
            job=job,
            cv_text=self.cv_text,
            profile=self.knowledge.profile,
            known_answers=self.knowledge.field_answers,
        )
        print(f"Fit score: {decision.fit_score}/100 | should_apply={decision.should_apply}")
        if decision.reasoning:
            print(f"Reason: {decision.reasoning}")

        outside_poland_required, outside_reason = self._requires_work_outside_poland(job, decision)
        if outside_poland_required:
            print("Skipped: role requires work location outside Poland.")
            if outside_reason:
                print(f"Location requirement: {outside_reason}")
            self.knowledge.save_application(
                ApplicationRecord(
                    url=job.url,
                    title=job.title,
                    company=job.company,
                    status="skipped_outside_poland",
                    notes=outside_reason or f"fit={decision.fit_score}",
                )
            )
            return

        if not self.settings.always_apply_except_outside_poland:
            for question in decision.missing_information:
                answer = self.knowledge.get_or_ask_answer(question, required=True)
                if answer:
                    self.knowledge.remember_answer(question, answer)

        write_tailored_cv_notes(self.settings.base_dir / "output" / "tailored_cv", job.job_id, decision.tailored_cv_notes)
        cover_letter_path = write_cover_letter(
            self.settings.base_dir / "output" / "cover_letters",
            job.job_id,
            decision.cover_letter,
            disclosure_text=self.settings.ai_disclosure_text,
            add_disclosure=self.settings.ai_disclosure_enabled,
        )

        should_apply = decision.should_apply and decision.fit_score >= self.settings.min_fit_score
        if self.settings.always_apply_except_outside_poland:
            should_apply = True
        if not should_apply:
            print("Skipped: below apply threshold.")
            self.knowledge.save_application(
                ApplicationRecord(
                    url=job.url,
                    title=job.title,
                    company=job.company,
                    status="skipped",
                    notes=f"fit={decision.fit_score}",
                )
            )
            return

        if self.settings.dry_run:
            print("Dry run enabled. No submit action taken.")
            self.knowledge.save_application(
                ApplicationRecord(
                    url=job.url,
                    title=job.title,
                    company=job.company,
                    status="dry_run_ready",
                    notes=f"mode={job.apply_mode}",
                )
            )
            return

        submitted = False
        if job.apply_mode == "easy":
            submitted = self._apply_easy(page, job, cover_letter_path, decision.prefilled_answers)
        elif job.apply_mode == "external" and self.settings.apply_external_forms:
            submitted = self._apply_external(context, page, job, cover_letter_path, decision.prefilled_answers)
        elif job.apply_mode == "unknown" and self.settings.apply_external_forms:
            print("Apply mode unknown. Trying Easy Apply first, then external/generic flow.")
            submitted = self._apply_easy(page, job, cover_letter_path, decision.prefilled_answers)
            if not submitted:
                submitted = self._apply_external(context, page, job, cover_letter_path, decision.prefilled_answers)
        else:
            print("Skipped: unsupported apply mode.")

        status = "submitted" if submitted else "not_submitted"
        notes = f"mode={job.apply_mode}"
        if self.last_apply_note:
            notes += f"; note={self.last_apply_note}"
        self.knowledge.save_application(
            ApplicationRecord(
                url=job.url,
                title=job.title,
                company=job.company,
                status=status,
                notes=notes,
            )
        )
        if submitted and self._unsave_job(page, job.url):
            print("Submitted job was removed from saved list.")
        print(f"Application result: {status}")

    def _read_job_posting(self, page: Page, job_url: str) -> JobPosting:
        self._goto_with_retries(page, job_url)
        page.wait_for_timeout(1500)
        self._expand_job_description(page)

        title = self._first_text(
            page,
            (
                "h1",
                "div.job-details-jobs-unified-top-card__job-title h1",
                "h1.top-card-layout__title",
            ),
            fallback="Unknown title",
        )
        company = self._first_text(
            page,
            (
                "div.job-details-jobs-unified-top-card__company-name a",
                "a.topcard__org-name-link",
                "span.jobs-unified-top-card__company-name",
            ),
            fallback="Unknown company",
        )
        location = self._first_text(
            page,
            (
                "div.job-details-jobs-unified-top-card__bullet",
                "span.topcard__flavor--bullet",
            ),
            fallback="",
        )
        description = self._first_text(
            page,
            (
                "div.jobs-description__content",
                "div.jobs-box__html-content",
                "div#job-details",
                "main",
            ),
            fallback="",
        )

        job_id = self._extract_job_id(job_url)
        apply_mode = self._detect_apply_mode(page)
        return JobPosting(
            job_id=job_id,
            url=job_url,
            title=title,
            company=company,
            location=location,
            description=description,
            apply_mode=apply_mode,
        )

    def _expand_job_description(self, page: Page) -> None:
        see_more = self._find_action(page, ("see more", "show more", "pokaz wiecej"), ())
        if see_more:
            try:
                see_more.click()
                page.wait_for_timeout(500)
            except Exception:
                pass

    def _find_easy_apply_trigger(self, page: Page) -> Locator | None:
        preferred_selectors = (
            "button[data-control-name*='inapply']",
            "button.jobs-apply-button--top-card",
            "button[aria-label*='easy apply' i]",
            "a[role='button'][aria-label*='easy apply' i]",
            "button:has-text('Easy Apply')",
            "button:has-text('Łatwe aplikowanie')",
            "button:has-text('Latwe aplikowanie')",
            "a[role='button']:has-text('Easy Apply')",
            "a[role='button']:has-text('Łatwe aplikowanie')",
            "a[role='button']:has-text('Latwe aplikowanie')",
        )
        for selector in preferred_selectors:
            nodes = page.locator(selector)
            max_scan = min(nodes.count(), 12)
            for idx in range(max_scan):
                node = nodes.nth(idx)
                try:
                    if node.is_visible() and node.is_enabled():
                        return node
                except Exception:
                    continue

        return self._find_action(page, self.EASY_APPLY_TOKENS, ())

    def _find_external_apply_trigger(self, page: Page) -> Locator | None:
        return self._find_action(
            page,
            self.EXTERNAL_APPLY_TOKENS,
            self.EASY_APPLY_TOKENS,
            allow_plain_anchors=True,
        )

    def _detect_apply_mode(self, page: Page) -> str:
        easy = self._find_easy_apply_trigger(page)
        if easy:
            return "easy"
        external = self._find_external_apply_trigger(page)
        if external:
            return "external"
        return "unknown"

    def _apply_easy(
        self,
        page: Page,
        job: JobPosting,
        cover_letter_path: Path | None,
        prefilled_answers: dict[str, str],
    ) -> bool:
        trigger = self._find_easy_apply_trigger(page)
        if not trigger and self.settings.copilot_mode:
            assist = self._await_human_assist(
                page=page,
                helper=None,
                stage="linkedin_easy_apply_open",
                reason="Nie znaleziono przycisku Easy Apply.",
            )
            if assist == "resume":
                trigger = self._find_easy_apply_trigger(page)
            elif assist == "skip":
                self.last_apply_note = "copilot_manual_skip"
                return False
            else:
                self.last_apply_note = "copilot_handoff_unavailable"
                return False
        if not trigger:
            print("Easy Apply button not found.")
            return False

        try:
            trigger.click()
        except Exception:
            if self.settings.copilot_mode:
                assist = self._await_human_assist(
                    page=page,
                    helper=None,
                    stage="linkedin_easy_apply_open",
                    reason="Nie udalo sie kliknac Easy Apply automatycznie.",
                )
                if assist == "resume":
                    page.wait_for_timeout(800)
                elif assist == "skip":
                    self.last_apply_note = "copilot_manual_skip"
                    return False
                else:
                    self.last_apply_note = "copilot_handoff_unavailable"
                    return False
            else:
                return False
        page.wait_for_timeout(1200)

        helper = FormHelper(
            knowledge=self.knowledge,
            cv_path=self.cv_path,
            cover_letter_path=cover_letter_path,
            prefilled_answers=prefilled_answers,
            ai_disclosure_text=self.settings.ai_disclosure_text if self.settings.ai_disclosure_enabled else "",
        )

        modal_root: Page | Locator = page
        action_history: list[str] = []
        manual_assists = 0
        state_repeat_count = 0
        last_state_signature = ""
        for _ in range(12):
            modal_root = self._easy_apply_action_root(page)
            self._inject_llm_form_answers(helper, page, job, stage="linkedin_easy_apply_modal")
            helper.fill_visible_fields(page)

            if self._easy_apply_success_detected(page):
                self._close_any_dialog(page, modal_root)
                return True

            repeated_action_before_step = self._repeated_action_tail(action_history)
            allow_llm_click = repeated_action_before_step < 3
            if allow_llm_click and self._click_llm_selected_action(
                root=modal_root,
                page=page,
                job=job,
                stage="linkedin_easy_apply_modal",
                allow_plain_anchors=False,
                helper=helper,
                action_history=action_history,
            ):
                page.wait_for_timeout(1000)
                if self._easy_apply_success_detected(page):
                    self._close_any_dialog(page, modal_root)
                    return True
                continue

            if self._click_action_with_confirmation(modal_root, self.SUBMIT_TOKENS, job):
                self._append_action_history(action_history, "submit")
                page.wait_for_timeout(1800)
                if self._easy_apply_success_detected(page):
                    self._close_any_dialog(page, modal_root)
                    return True

            if self._click_action(modal_root, self.REVIEW_TOKENS, ()):
                self._append_action_history(action_history, "review")
                page.wait_for_timeout(1000)
                continue
            if self._click_action(modal_root, self.NEXT_TOKENS, ()):
                self._append_action_history(action_history, "next")
                page.wait_for_timeout(1000)
                continue

            validation_messages = self._collect_validation_messages(page)
            visible_fields = helper.collect_visible_fields_snapshot(page)
            candidates, locator_by_id = self._collect_action_candidates(modal_root, allow_plain_anchors=False)
            state_signature = self._build_copilot_state_signature(
                stage="linkedin_easy_apply_modal",
                page=page,
                visible_fields=visible_fields,
                validation_messages=validation_messages,
                candidates=candidates,
            )
            if state_signature and state_signature == last_state_signature:
                state_repeat_count += 1
            else:
                state_repeat_count = 1 if state_signature else 0
                last_state_signature = state_signature

            if self._try_copilot_memory_action(
                state_signature=state_signature,
                candidates=candidates,
                locator_by_id=locator_by_id,
                action_history=action_history,
            ):
                page.wait_for_timeout(900)
                if self._easy_apply_success_detected(page):
                    self._close_any_dialog(page, modal_root)
                    return True
                continue

            repeated_action_count = self._repeated_action_tail(action_history)
            should_trigger_stuck = self._should_trigger_stuck_recovery(
                state_repeat_count=state_repeat_count,
                repeated_action_count=repeated_action_count,
            ) or not candidates
            if should_trigger_stuck:
                page_signals = self._collect_page_signals(page, visible_fields, validation_messages)
                recovery = self._try_llm_stuck_recovery(
                    page=page,
                    job=job,
                    stage="linkedin_easy_apply_modal",
                    state_signature=state_signature,
                    root=modal_root,
                    allow_plain_anchors=False,
                    helper=helper,
                    action_history=action_history,
                )
                if recovery in {"clicked", "retry"}:
                    page.wait_for_timeout(900)
                    if self._easy_apply_success_detected(page):
                        self._close_any_dialog(page, modal_root)
                        return True
                    continue
                if recovery == "human":
                    if manual_assists < 4:
                        assist = self._await_human_assist(
                            page=page,
                            helper=helper,
                            stage="linkedin_easy_apply_modal",
                            reason="LLM requested manual intervention for blocked flow.",
                            validation_messages=validation_messages,
                            state_signature=state_signature,
                            action_candidates=candidates,
                        )
                        if assist == "resume":
                            manual_assists += 1
                            page.wait_for_timeout(700)
                            if self._easy_apply_success_detected(page):
                                self._close_any_dialog(page, modal_root)
                                return True
                            continue
                        if assist == "skip":
                            self.last_apply_note = "copilot_manual_skip"
                        else:
                            self.last_apply_note = "copilot_handoff_unavailable"
                    elif self.settings.copilot_mode and not self.last_apply_note:
                        self.last_apply_note = "copilot_assist_limit_reached"
                    break

            if manual_assists < 4:
                assist = self._await_human_assist(
                    page=page,
                    helper=helper,
                    stage="linkedin_easy_apply_modal",
                    reason="Brak dalszej akcji automatycznej (np. ukryte wymagane pole lub niestandardowy przycisk).",
                    validation_messages=validation_messages,
                    state_signature=state_signature,
                    action_candidates=candidates,
                )
                if assist == "resume":
                    manual_assists += 1
                    page.wait_for_timeout(700)
                    if self._easy_apply_success_detected(page):
                        self._close_any_dialog(page, modal_root)
                        return True
                    continue
                if assist == "skip":
                    self.last_apply_note = "copilot_manual_skip"
                else:
                    self.last_apply_note = "copilot_handoff_unavailable"
            elif self.settings.copilot_mode and not self.last_apply_note:
                self.last_apply_note = "copilot_assist_limit_reached"
            break

        self._close_any_dialog(page, modal_root)
        return False

    def _apply_external(
        self,
        context: BrowserContext,
        page: Page,
        job: JobPosting,
        cover_letter_path: Path | None,
        prefilled_answers: dict[str, str],
    ) -> bool:
        trigger = self._find_external_apply_trigger(page)
        external_page: Page | None = None
        if not trigger:
            print("External apply button not found. Trying to interact on current page.")
            if self.settings.copilot_mode:
                assist = self._await_human_assist(
                    page=page,
                    helper=None,
                    stage="external_apply_open",
                    reason="Nie znaleziono przycisku Apply. Otworz formularz recznie i wznow.",
                )
                if assist == "skip":
                    self.last_apply_note = "copilot_manual_skip"
                    return False
                if assist == "resume":
                    external_page = page
                else:
                    self.last_apply_note = "copilot_handoff_unavailable"
                    return False
            else:
                return False

        opened_in_new_tab = False
        if trigger and external_page is None:
            try:
                with context.expect_page(timeout=6000) as event_info:
                    trigger.click()
                external_page = event_info.value
                opened_in_new_tab = True
                external_page.wait_for_load_state("domcontentloaded")
            except PlaywrightTimeoutError:
                previous_url = page.url
                try:
                    trigger.click()
                except Exception:
                    if self.settings.copilot_mode:
                        assist = self._await_human_assist(
                            page=page,
                            helper=None,
                            stage="external_apply_open",
                            reason="Nie udalo sie kliknac przycisku Apply automatycznie.",
                        )
                        if assist == "skip":
                            self.last_apply_note = "copilot_manual_skip"
                            return False
                        if assist == "resume":
                            external_page = page
                        else:
                            self.last_apply_note = "copilot_handoff_unavailable"
                            return False
                    else:
                        return False
                page.wait_for_timeout(2000)
                if page.url != previous_url:
                    external_page = page
            except Exception:
                if self.settings.copilot_mode:
                    assist = self._await_human_assist(
                        page=page,
                        helper=None,
                        stage="external_apply_open",
                        reason="Blad podczas otwierania zewnetrznego formularza. Sprobuj recznie i wznow.",
                    )
                    if assist == "skip":
                        self.last_apply_note = "copilot_manual_skip"
                        return False
                    if assist == "resume":
                        external_page = page
                    else:
                        self.last_apply_note = "copilot_handoff_unavailable"
                        return False
                else:
                    return False

        if external_page is None:
            external_page = page

        if "linkedin.com" in external_page.url.lower():
            easy_apply = self._find_easy_apply_trigger(external_page)
            if easy_apply:
                print("Detected Easy Apply after apply click. Switching to Easy Apply flow.")
                return self._apply_easy(external_page, job, cover_letter_path, prefilled_answers)

        if self._wait_and_detect_external_login_gate(external_page):
            if self.settings.copilot_mode:
                assist = self._await_human_assist(
                    page=external_page,
                    helper=None,
                    stage="external_application_form",
                    reason="Zewnetrzny formularz wymaga dodatkowego logowania lub potwierdzenia tozsamosci.",
                )
                if assist == "resume":
                    external_page.wait_for_timeout(700)
                elif assist == "skip":
                    self.last_apply_note = "copilot_manual_skip"
                    return False
                else:
                    self.last_apply_note = "copilot_handoff_unavailable"
                    return False

            if self._wait_and_detect_external_login_gate(external_page):
                self.last_apply_note = "requires_external_login"
                print("Skipped: external application requires login outside LinkedIn.")
                if opened_in_new_tab and external_page is not page:
                    try:
                        external_page.close()
                    except Exception:
                        pass
                return False

        helper = FormHelper(
            knowledge=self.knowledge,
            cv_path=self.cv_path,
            cover_letter_path=cover_letter_path,
            prefilled_answers=prefilled_answers,
            ai_disclosure_text=self.settings.ai_disclosure_text if self.settings.ai_disclosure_enabled else "",
        )

        submitted = False
        action_history: list[str] = []
        manual_assists = 0
        state_repeat_count = 0
        last_state_signature = ""
        for _ in range(16):
            if self._is_page_closed(external_page):
                return False

            if self._wait_and_detect_external_login_gate(external_page):
                if self.settings.copilot_mode:
                    assist = self._await_human_assist(
                        page=external_page,
                        helper=helper,
                        stage="external_application_form",
                        reason="Formularz wymaga recznej akcji (np. login/captcha/weryfikacja).",
                    )
                    if assist == "resume":
                        external_page.wait_for_timeout(700)
                        if self._wait_and_detect_external_login_gate(external_page):
                            self.last_apply_note = "requires_external_login"
                            print("Skipped: external application requires login outside LinkedIn.")
                            return False
                    elif assist == "skip":
                        self.last_apply_note = "copilot_manual_skip"
                        return False
                    else:
                        self.last_apply_note = "requires_external_login"
                        print("Skipped: external application requires login outside LinkedIn.")
                        return False
                else:
                    self.last_apply_note = "requires_external_login"
                    print("Skipped: external application requires login outside LinkedIn.")
                    return False

            self._inject_llm_form_answers(helper, external_page, job, stage="external_application_form")
            helper.fill_visible_fields(external_page)
            if self._external_apply_success_detected(external_page):
                submitted = True
                break

            repeated_action_before_step = self._repeated_action_tail(action_history)
            allow_llm_click = repeated_action_before_step < 3
            if allow_llm_click and self._click_llm_selected_action(
                root=external_page,
                page=external_page,
                job=job,
                stage="external_application_form",
                allow_plain_anchors=True,
                helper=helper,
                action_history=action_history,
            ):
                external_page.wait_for_timeout(1000)
                if self._external_apply_success_detected(external_page):
                    submitted = True
                    break
                continue

            if self._click_action_with_confirmation(external_page, self.SUBMIT_TOKENS, job):
                self._append_action_history(action_history, "submit")
                external_page.wait_for_timeout(1200)
                if self._external_apply_success_detected(external_page):
                    submitted = True
                    break
                continue
            if self._click_action(external_page, self.NEXT_TOKENS, ()):
                self._append_action_history(action_history, "next")
                external_page.wait_for_timeout(900)
                continue
            if self._click_action(external_page, self.REVIEW_TOKENS, ()):
                self._append_action_history(action_history, "review")
                external_page.wait_for_timeout(900)
                continue
            if self._click_external_progress_action(external_page):
                self._append_action_history(action_history, "progress")
                external_page.wait_for_timeout(900)
                continue
            if self._click_external_primary_fallback(external_page):
                self._append_action_history(action_history, "primary_fallback")
                external_page.wait_for_timeout(900)
                continue

            validation_messages = self._collect_validation_messages(external_page)
            visible_fields = helper.collect_visible_fields_snapshot(external_page)
            candidates, locator_by_id = self._collect_action_candidates(external_page, allow_plain_anchors=True)
            state_signature = self._build_copilot_state_signature(
                stage="external_application_form",
                page=external_page,
                visible_fields=visible_fields,
                validation_messages=validation_messages,
                candidates=candidates,
            )
            if state_signature and state_signature == last_state_signature:
                state_repeat_count += 1
            else:
                state_repeat_count = 1 if state_signature else 0
                last_state_signature = state_signature

            if self._try_copilot_memory_action(
                state_signature=state_signature,
                candidates=candidates,
                locator_by_id=locator_by_id,
                action_history=action_history,
            ):
                external_page.wait_for_timeout(900)
                if self._external_apply_success_detected(external_page):
                    submitted = True
                    break
                continue

            repeated_action_count = self._repeated_action_tail(action_history)
            should_trigger_stuck = self._should_trigger_stuck_recovery(
                state_repeat_count=state_repeat_count,
                repeated_action_count=repeated_action_count,
            ) or not candidates
            if should_trigger_stuck:
                page_signals = self._collect_page_signals(external_page, visible_fields, validation_messages)
                recovery = self._try_llm_stuck_recovery(
                    page=external_page,
                    job=job,
                    stage="external_application_form",
                    state_signature=state_signature,
                    root=external_page,
                    allow_plain_anchors=True,
                    helper=helper,
                    action_history=action_history,
                )
                if recovery in {"clicked", "retry"}:
                    external_page.wait_for_timeout(900)
                    if self._external_apply_success_detected(external_page):
                        submitted = True
                        break
                    continue
                if recovery == "human":
                    if manual_assists < 5:
                        assist = self._await_human_assist(
                            page=external_page,
                            helper=helper,
                            stage="external_application_form",
                            reason="LLM requested manual intervention for blocked flow.",
                            validation_messages=validation_messages,
                            state_signature=state_signature,
                            action_candidates=candidates,
                        )
                        if assist == "resume":
                            manual_assists += 1
                            external_page.wait_for_timeout(700)
                            if self._external_apply_success_detected(external_page):
                                submitted = True
                                break
                            continue
                        if assist == "skip":
                            self.last_apply_note = "copilot_manual_skip"
                        else:
                            self.last_apply_note = "copilot_handoff_unavailable"
                    elif self.settings.copilot_mode and not self.last_apply_note:
                        self.last_apply_note = "copilot_assist_limit_reached"
                    if not self.last_apply_note:
                        self.last_apply_note = "copilot_stuck_external_form"
                    break

            if manual_assists < 5:
                assist = self._await_human_assist(
                    page=external_page,
                    helper=helper,
                    stage="external_application_form",
                    reason="Brak dalszej akcji automatycznej. Uzupelnij brakujace kroki recznie i wznow.",
                    validation_messages=validation_messages,
                    state_signature=state_signature,
                    action_candidates=candidates,
                )
                if assist == "resume":
                    manual_assists += 1
                    external_page.wait_for_timeout(700)
                    if self._external_apply_success_detected(external_page):
                        submitted = True
                        break
                    continue
                if assist == "skip":
                    self.last_apply_note = "copilot_manual_skip"
                else:
                    self.last_apply_note = "copilot_handoff_unavailable"
            elif self.settings.copilot_mode and not self.last_apply_note:
                self.last_apply_note = "copilot_assist_limit_reached"
            if not self.last_apply_note:
                self.last_apply_note = "copilot_stuck_external_form"
            break

        try:
            # Avoid closing tabs when submission did not complete; this helps manual recovery.
            if opened_in_new_tab and submitted and external_page is not page:
                external_page.close()
        except Exception:
            pass
        return submitted

    def _click_external_progress_action(self, page: Page) -> bool:
        action = self._find_action(
            page,
            self.EXTERNAL_PROGRESS_TOKENS,
            self.EXTERNAL_EXCLUDE_TOKENS,
            allow_plain_anchors=True,
        )
        if not action:
            return False
        try:
            action.click()
            return True
        except Exception:
            return False

    @staticmethod
    def _append_action_history(action_history: list[str] | None, label: str, max_items: int = 12) -> None:
        if action_history is None:
            return
        cleaned = str(label).strip()
        if not cleaned:
            return
        action_history.append(cleaned)
        if len(action_history) > max_items:
            del action_history[:-max_items]

    @staticmethod
    def _click_external_primary_fallback(page: Page) -> bool:
        try:
            if page.is_closed():
                return False
        except Exception:
            return False

        selectors = (
            "button[type='submit']",
            "input[type='submit']",
            "button[class*='primary']",
            "a[role='button'][class*='primary']",
        )
        for selector in selectors:
            locator = page.locator(selector)
            try:
                max_scan = min(locator.count(), 20)
            except Exception:
                try:
                    if page.is_closed():
                        return False
                except Exception:
                    return False
                continue
            for idx in range(max_scan):
                node = locator.nth(idx)
                try:
                    if not node.is_visible() or not node.is_enabled():
                        continue
                    node.click()
                    return True
                except Exception:
                    continue
        return False

    def _click_llm_selected_action(
        self,
        root: Page | Locator,
        page: Page,
        job: JobPosting,
        stage: str,
        allow_plain_anchors: bool,
        helper: FormHelper | None = None,
        action_history: list[str] | None = None,
    ) -> bool:
        candidates, locator_by_id = self._collect_action_candidates(root, allow_plain_anchors)
        if not candidates:
            return False

        visible_fields: list[dict[str, str | bool | list[str]]] | list[dict] = []
        validation_messages: list[str] = []
        page_signals: dict[str, str | int | bool | list[str]] = {}
        if helper is not None:
            visible_fields = helper.collect_visible_fields_snapshot(page)
            validation_messages = self._collect_validation_messages(page)
            page_signals = self._collect_page_signals(page, visible_fields, validation_messages)

        choice_id, reason = self.llm.choose_action_button(
            job=job,
            page_url=page.url,
            stage=stage,
            candidates=candidates,
            visible_fields=visible_fields,
            validation_messages=validation_messages,
            page_signals=page_signals,
            recent_actions=action_history or [],
        )
        if choice_id is None:
            return False

        chosen_locator = locator_by_id.get(choice_id)
        if chosen_locator is None:
            return False

        try:
            chosen_label = next(
                (
                    item.get("label", "")
                    for item in candidates
                    if int(item.get("id", -1)) == choice_id
                ),
                "",
            )
            if chosen_label:
                if reason:
                    print(f"LLM click: {chosen_label} | {reason}")
                else:
                    print(f"LLM click: {chosen_label}")
            self._append_action_history(action_history, chosen_label or f"id={choice_id}")
            chosen_locator.click()
            return True
        except Exception:
            return False

    def _collect_action_candidates(
        self,
        root: Page | Locator,
        allow_plain_anchors: bool,
    ) -> tuple[list[dict[str, str]], dict[int, Locator]]:
        selector = "button, a[role='button'], input[type='submit'], input[type='button']"
        if allow_plain_anchors:
            selector += ", a"
        nodes = root.locator(selector)
        try:
            max_scan = min(nodes.count(), 250)
        except Exception:
            return [], {}

        candidates: list[dict[str, str]] = []
        locator_by_id: dict[int, Locator] = {}
        for index in range(max_scan):
            node = nodes.nth(index)
            try:
                if not node.is_visible() or not node.is_enabled():
                    continue
            except Exception:
                continue

            label = self._node_label(node)
            if not label:
                continue

            role = (node.get_attribute("role") or "").strip()
            node_type = (node.get_attribute("type") or "").strip()
            href = (node.get_attribute("href") or "").strip()
            candidate_id = len(candidates)

            candidates.append(
                {
                    "id": str(candidate_id),
                    "label": label[:160],
                    "role": role[:40],
                    "type": node_type[:40],
                    "href": href[:220],
                }
            )
            locator_by_id[candidate_id] = node
            if len(candidates) >= 40:
                break
        return candidates, locator_by_id

    @staticmethod
    def _node_label(node: Locator) -> str:
        try:
            text = (node.inner_text() or "").strip()
        except Exception:
            text = ""
        if text:
            return " ".join(text.split())

        for attribute in ("value", "aria-label", "title", "name"):
            try:
                value = (node.get_attribute(attribute) or "").strip()
            except Exception:
                value = ""
            if value:
                return " ".join(value.split())
        return ""

    def _click_action_with_confirmation(self, root: Page | Locator, tokens: tuple[str, ...], job: JobPosting) -> bool:
        action = self._find_action(root, tokens, (), allow_plain_anchors=False)
        if not action:
            return False
        if not self._can_submit(job):
            return False
        try:
            action.click()
            return True
        except Exception:
            return False

    def _click_action(self, root: Page | Locator, tokens: tuple[str, ...], exclude_tokens: tuple[str, ...]) -> bool:
        action = self._find_action(root, tokens, exclude_tokens, allow_plain_anchors=False)
        if not action:
            return False
        try:
            action.click()
            return True
        except Exception:
            return False

    def _find_action(
        self,
        root: Page | Locator,
        tokens: tuple[str, ...],
        exclude_tokens: tuple[str, ...],
        allow_plain_anchors: bool = False,
    ) -> Locator | None:
        lookup_tokens = tuple(_normalize(token) for token in tokens)
        lookup_exclusions = tuple(_normalize(token) for token in exclude_tokens)
        selector = "button, a[role='button']"
        if allow_plain_anchors:
            selector += ", a"
        candidates = root.locator(selector)
        try:
            max_scan = min(candidates.count(), 300)
        except Exception:
            return None

        for index in range(max_scan):
            node = candidates.nth(index)
            try:
                if not node.is_visible():
                    continue
            except Exception:
                continue

            try:
                label = self._node_label(node)
            except Exception:
                label = ""
            normalized_label = _normalize(label)
            if not normalized_label:
                continue

            if any(exclusion and exclusion in normalized_label for exclusion in lookup_exclusions):
                continue
            if any(token and token in normalized_label for token in lookup_tokens):
                return node
        return None

    def _easy_apply_success_detected(self, page: Page) -> bool:
        markers = (
            "application submitted",
            "aplikacja wyslana",
            "you successfully applied",
            "applied",
        )
        page_text = _normalize(page.locator("body").inner_text()[:4000])
        return any(marker in page_text for marker in markers)

    def _external_apply_success_detected(self, page: Page) -> bool:
        markers = (
            "application submitted",
            "you have successfully applied",
            "thank you for applying",
            "thanks for applying",
            "application received",
            "your application has been sent",
            "we have received your application",
            "aplikacja wyslana",
            "dziekujemy za aplikowanie",
        )
        try:
            page_text = _normalize(page.locator("body").inner_text()[:6000])
        except Exception:
            return False
        return any(marker in page_text for marker in markers)

    def _close_any_dialog(self, page: Page, root: Page | Locator | None = None) -> None:
        target: Page | Locator = root or page
        if self._click_action(target, self.CLOSE_TOKENS, ()):
            page.wait_for_timeout(600)
        self._click_action(target, self.DISCARD_TOKENS, ())

    def _requires_external_login(self, page: Page) -> bool:
        url = page.url.lower()
        if "linkedin.com" in url:
            return False
        if any(token in url for token in ("/login", "/signin", "/sign-in", "/account/login", "/auth/")):
            return True
        has_password = page.locator("input[type='password'], input[name*='password']").count() > 0
        has_identity = page.locator(
            "input[type='email'], input[name*='email'], input[name*='user'], input[name*='login']"
        ).count() > 0
        if has_password and has_identity:
            return True
        body_text = ""
        try:
            body_text = _normalize(page.locator("body").inner_text()[:3000])
        except Exception:
            return False
        login_tokens = (
            "sign in",
            "log in",
            "login",
            "create account",
            "register to apply",
            "continue with google",
            "continue with email",
            "already have an account",
        )
        has_login_copy = any(token in body_text for token in login_tokens)
        return has_login_copy and (has_password or has_identity)

    def _unsave_job(self, page: Page, job_url: str) -> bool:
        try:
            self._goto_with_retries(page, job_url)
            page.wait_for_timeout(900)
        except Exception:
            return False

        controls = page.locator("button, a[role='button']")
        for idx in range(min(controls.count(), 120)):
            node = controls.nth(idx)
            try:
                if not node.is_visible() or not node.is_enabled():
                    continue
            except Exception:
                continue

            label = _normalize(self._node_label(node))
            if not label:
                continue
            pressed = (node.get_attribute("aria-pressed") or "").strip().lower()
            if pressed == "true" or any(token in label for token in self.UNSAVE_TOKENS):
                try:
                    node.click()
                    page.wait_for_timeout(500)
                    return True
                except Exception:
                    continue
        return False

    def _can_submit(self, job: JobPosting) -> bool:
        return True

    @staticmethod
    def _first_text(page: Page, selectors: tuple[str, ...], fallback: str = "") -> str:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() == 0:
                    continue
                text = (locator.inner_text() or "").strip()
                if text:
                    return text
            except Exception:
                continue
        return fallback

    @staticmethod
    def _extract_job_id(job_url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", job_url)
        if match:
            return match.group(1)
        digest = hashlib.sha1(job_url.encode("utf-8")).hexdigest()
        return digest[:12]

    @staticmethod
    def _requires_work_outside_poland(job: JobPosting, decision: FitDecision) -> tuple[bool, str]:
        if getattr(decision, "requires_work_outside_poland", False):
            return True, (getattr(decision, "location_restriction_reasoning", "") or "").strip()

        location = _normalize(job.location)
        description = _normalize(job.description[:4000])

        poland_tokens = (
            "poland",
            "polska",
            "warsaw",
            "warszawa",
            "krakow",
            "wroclaw",
            "poznan",
            "gdansk",
            "katowice",
            "lodz",
        )
        if any(token in location for token in poland_tokens):
            return False, ""

        remote_tokens = ("remote", "zdal", "hybrid", "eu", "emea")
        if any(token in location for token in remote_tokens):
            return False, ""

        restriction_clues = (
            "must be based in",
            "on site in",
            "onsite in",
            "relocation to",
            "work from our office in",
            "must relocate",
            "required to be located in",
        )
        outside_country_tokens = (
            "germany",
            "france",
            "spain",
            "netherlands",
            "uk",
            "united kingdom",
            "ireland",
            "sweden",
            "norway",
            "denmark",
            "finland",
            "portugal",
            "italy",
            "switzerland",
            "austria",
            "czech",
            "hungary",
            "romania",
            "bulgaria",
            "usa",
            "united states",
            "canada",
            "australia",
        )
        if any(clue in description for clue in restriction_clues) and any(token in description for token in outside_country_tokens):
            return True, "Description explicitly requires work location outside Poland."

        if location and not any(token in location for token in poland_tokens + remote_tokens):
            if any(token in location for token in outside_country_tokens):
                return True, f"Job location appears to be outside Poland: {job.location}"

        return False, ""

    @staticmethod
    def _easy_apply_action_root(page: Page) -> Page | Locator:
        dialogs = page.locator("div[role='dialog']")
        max_scan = min(dialogs.count(), 5)
        for reverse_index in range(max_scan):
            idx = max_scan - 1 - reverse_index
            dialog = dialogs.nth(idx)
            try:
                if dialog.is_visible():
                    return dialog
            except Exception:
                continue
        return page

    @staticmethod
    def _goto_with_retries(page: Page, url: str, attempts: int = 3) -> None:
        last_error: Exception | None = None
        for attempt in range(attempts):
            if LinkedInJobApplier._is_page_closed(page):
                if last_error is not None:
                    raise last_error
                raise RuntimeError("Browser page was closed before navigation completed.")
            try:
                page.goto(url, wait_until="domcontentloaded")
                return
            except Exception as exc:  # Playwright raises multiple error types here.
                last_error = exc
                if LinkedInJobApplier._is_page_closed(page):
                    break
                try:
                    page.wait_for_timeout(600 + 400 * attempt)
                except Exception:
                    break
        if last_error is not None:
            raise last_error

    def _wait_and_detect_external_login_gate(self, page: Page) -> bool:
        for _ in range(6):
            if self._is_page_closed(page):
                return False
            if self._requires_external_login(page):
                return True
            if self._external_apply_success_detected(page):
                return False
            try:
                page.wait_for_timeout(350)
            except Exception:
                return False
        return self._requires_external_login(page)

    def _inject_llm_form_answers(self, helper: FormHelper, page: Page, job: JobPosting, stage: str) -> None:
        fields = helper.collect_visible_fields_snapshot(page)
        if not fields:
            return
        validation_messages = self._collect_validation_messages(page)
        page_signals = self._collect_page_signals(page, fields, validation_messages)
        llm_answers = self.llm.propose_form_answers(
            job=job,
            page_url=page.url,
            stage=stage,
            visible_fields=fields,
            profile=self.knowledge.profile,
            known_answers=self.knowledge.field_answers,
            cv_text=self.cv_text,
            validation_messages=validation_messages,
            page_signals=page_signals,
        )
        if llm_answers:
            helper.add_prefilled_answers(llm_answers)

    @staticmethod
    def _is_context_closed(context: BrowserContext | None) -> bool:
        if context is None:
            return True
        try:
            _ = context.pages
            return False
        except Exception:
            return True

    @staticmethod
    def _is_page_closed(page: Page) -> bool:
        try:
            return page.is_closed()
        except Exception:
            return True

    @staticmethod
    def _is_target_closed_exception(exc: Exception) -> bool:
        class_name = type(exc).__name__.strip().lower()
        if "targetclosed" in class_name:
            return True
        message = " ".join(str(exc).split()).lower()
        markers = (
            "target page, context or browser has been closed",
            "target closed",
            "execution context was destroyed",
        )
        return any(marker in message for marker in markers)

    @staticmethod
    def _snapshot_field_identity(field: dict[str, Any]) -> str:
        kind = _normalize(str(field.get("kind", "")))
        label = _normalize(str(field.get("label", "")))
        context = _normalize(str(field.get("context", ""))[:220])
        return f"{kind}|{label}|{context}"

    @staticmethod
    def _snapshot_fingerprint(snapshot: list[dict[str, Any]]) -> str:
        compact: list[dict[str, str | bool]] = []
        for field in snapshot:
            compact.append(
                {
                    "k": _normalize(str(field.get("kind", ""))),
                    "l": _normalize(str(field.get("label", ""))),
                    "v": _normalize(str(field.get("current_value", ""))),
                    "r": bool(field.get("required", False)),
                }
            )
        compact.sort(key=lambda item: f"{item.get('k')}|{item.get('l')}")
        payload = json.dumps(compact, sort_keys=True, ensure_ascii=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _build_copilot_state_signature(
        self,
        stage: str,
        page: Page,
        visible_fields: list[dict[str, Any]],
        validation_messages: list[str],
        candidates: list[dict[str, str]],
    ) -> str:
        parsed_url = urlparse(page.url)
        host = parsed_url.netloc.lower()
        path = re.sub(r"\d+", "{n}", parsed_url.path.lower())[:180]

        required_unanswered: list[str] = []
        field_labels: list[str] = []
        for field in visible_fields[:40]:
            label = _normalize(str(field.get("label", "")))[:120]
            if not label:
                continue
            field_labels.append(label)
            current_value = str(field.get("current_value", "")).strip()
            if bool(field.get("required", False)) and not current_value:
                required_unanswered.append(label)

        validation = [_normalize(message)[:120] for message in validation_messages[:6] if message.strip()]
        action_labels = [_normalize(str(candidate.get("label", "")))[:120] for candidate in candidates[:15]]

        payload = {
            "stage": stage,
            "host": host,
            "path": path,
            "required_unanswered": sorted(required_unanswered)[:20],
            "field_labels": sorted(field_labels)[:25],
            "validation": validation,
            "actions": action_labels,
        }
        digest = hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()[:20]
        return f"{stage}:{digest}"

    @staticmethod
    def _ensure_interaction_tracker(page: Page) -> None:
        try:
            page.evaluate(
                """() => {
                    if (window.__copilotTrackerInstalled) return true;
                    window.__copilotTrackerInstalled = true;
                    window.__copilotEventLog = [];

                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
                    const pickTarget = (node) => {
                        if (!(node instanceof Element)) return null;
                        return node.closest('button, a, input, select, textarea, [role="button"], [role="option"], label') || node;
                    };
                    const describe = (node) => {
                        const target = pickTarget(node);
                        if (!target) return null;
                        return {
                            tag: clean((target.tagName || '').toLowerCase()),
                            label: clean(target.innerText || target.textContent || target.value || ''),
                            ariaLabel: clean(target.getAttribute('aria-label') || ''),
                            name: clean(target.getAttribute('name') || ''),
                            id: clean(target.id || ''),
                            role: clean(target.getAttribute('role') || ''),
                        };
                    };
                    const push = (kind, node) => {
                        const info = describe(node);
                        if (!info) return;
                        window.__copilotEventLog.push({
                            ts: Date.now(),
                            kind,
                            url: location.href,
                            ...info,
                        });
                        if (window.__copilotEventLog.length > 500) {
                            window.__copilotEventLog = window.__copilotEventLog.slice(-500);
                        }
                    };

                    document.addEventListener('click', (event) => push('click', event.target), true);
                    document.addEventListener('change', (event) => push('change', event.target), true);
                    document.addEventListener('input', (event) => {
                        const target = event.target;
                        if (target instanceof Element && target.matches('input, textarea')) {
                            push('input', target);
                        }
                    }, true);
                    return true;
                }"""
            )
        except Exception:
            return

    def _interaction_cursor(self, page: Page) -> int:
        self._ensure_interaction_tracker(page)
        try:
            value = page.evaluate("() => Array.isArray(window.__copilotEventLog) ? window.__copilotEventLog.length : 0")
            return int(value)
        except Exception:
            return 0

    def _interaction_events_since(self, page: Page, cursor: int) -> tuple[list[dict[str, str]], int]:
        self._ensure_interaction_tracker(page)
        try:
            raw = page.evaluate(
                """(cursor) => {
                    const events = Array.isArray(window.__copilotEventLog) ? window.__copilotEventLog : [];
                    const safeCursor = Math.max(0, Math.min(Number(cursor) || 0, events.length));
                    const delta = events.slice(safeCursor);
                    return { delta, cursor: events.length };
                }""",
                cursor,
            )
        except Exception:
            return [], cursor

        if not isinstance(raw, dict):
            return [], cursor
        delta_raw = raw.get("delta", [])
        next_cursor_raw = raw.get("cursor", cursor)
        next_cursor = int(next_cursor_raw) if isinstance(next_cursor_raw, int | float) else cursor

        events: list[dict[str, str]] = []
        if isinstance(delta_raw, list):
            for item in delta_raw:
                if not isinstance(item, dict):
                    continue
                events.append(
                    {
                        "kind": str(item.get("kind", "")).strip(),
                        "label": str(item.get("label", "")).strip(),
                        "ariaLabel": str(item.get("ariaLabel", "")).strip(),
                        "name": str(item.get("name", "")).strip(),
                        "id": str(item.get("id", "")).strip(),
                        "role": str(item.get("role", "")).strip(),
                    }
                )
        return events, next_cursor

    @staticmethod
    def _event_candidate_text(event: dict[str, str]) -> str:
        combined = " ".join(
            (
                event.get("label", ""),
                event.get("ariaLabel", ""),
                event.get("name", ""),
                event.get("id", ""),
                event.get("role", ""),
            )
        )
        return _normalize(combined)[:180]

    def _match_event_to_candidate(self, event: dict[str, str], candidates: list[dict[str, str]]) -> int | None:
        event_text = self._event_candidate_text(event)
        if len(event_text) < 2:
            return None

        best_index: int | None = None
        best_score = 0.0
        for index, candidate in enumerate(candidates):
            candidate_label = _normalize(str(candidate.get("label", "")))[:180]
            if not candidate_label:
                continue
            if event_text == candidate_label:
                return index
            if event_text in candidate_label or candidate_label in event_text:
                return index
            score = SequenceMatcher(None, event_text, candidate_label).ratio()
            if score > best_score:
                best_score = score
                best_index = index

        if best_index is not None and best_score >= 0.72:
            return best_index
        return None

    def _learn_recipe_from_human_events(
        self,
        state_signature: str,
        action_candidates: list[dict[str, str]],
        events: list[dict[str, str]],
    ) -> None:
        if not state_signature or not action_candidates or not events:
            return

        for event in reversed(events):
            if event.get("kind", "").strip().lower() not in {"click", "change"}:
                continue
            match_index = self._match_event_to_candidate(event, action_candidates)
            if match_index is None:
                continue
            try:
                label = str(action_candidates[match_index].get("label", "")).strip()
            except Exception:
                label = ""
            if not label:
                continue
            self.knowledge.remember_copilot_recipe(
                state_signature=state_signature,
                action_label=label,
                source="human_event",
            )
            print(f"[Copilot] Learned recovery action: {label}")
            return

    def _learn_agentic_playbook_from_human_events(
        self,
        state_signature: str,
        stage: str,
        action_candidates: list[dict[str, str]] | None,
        events: list[dict[str, str]],
    ) -> None:
        if not state_signature or not stage or not events:
            return

        candidates = action_candidates or []
        steps: list[dict[str, Any]] = []
        saw_field_change = False

        for event in events:
            kind = event.get("kind", "").strip().lower()
            if kind == "click":
                match_index = self._match_event_to_candidate(event, candidates) if candidates else None
                if match_index is None:
                    continue
                label = ""
                try:
                    label = str(candidates[match_index].get("label", "")).strip()
                except Exception:
                    label = ""
                if not label:
                    continue

                click_step = {
                    "tool": "click_action",
                    "payload": {
                        "candidate_id": int(match_index),
                        "label": label,
                    },
                }
                if steps:
                    prev = steps[-1]
                    if (
                        isinstance(prev, dict)
                        and prev.get("tool") == "click_action"
                        and str((prev.get("payload") or {}).get("label", "")).strip() == label
                    ):
                        continue
                steps.append(click_step)
                steps.append({"tool": "wait", "payload": {"ms": 700}})
                continue

            if kind in {"change", "input"}:
                saw_field_change = True

        if saw_field_change and not any(str(step.get("tool", "")) == "fill_visible_fields" for step in steps):
            steps.insert(0, {"tool": "fill_visible_fields", "payload": {}})
            if len(steps) > 1 and str(steps[1].get("tool", "")) != "wait":
                steps.insert(1, {"tool": "wait", "payload": {"ms": 500}})

        while steps and str(steps[-1].get("tool", "")) == "wait":
            steps.pop()

        if not steps:
            return

        steps = steps[:24]
        fingerprint = self.knowledge.remember_agentic_playbook(
            state_signature=state_signature,
            stage=stage,
            steps=steps,
            source="human_event_sequence",
            notes="learned from manual handoff events",
        )
        if not fingerprint:
            return

        self.knowledge.mark_agentic_playbook_result(
            state_signature=state_signature,
            stage=stage,
            step_fingerprint=fingerprint,
            success=True,
        )
        print(f"[Copilot] Learned agentic playbook from manual events ({len(steps)} steps).")

    def _try_copilot_memory_action(
        self,
        state_signature: str,
        candidates: list[dict[str, str]],
        locator_by_id: dict[int, Locator],
        action_history: list[str] | None = None,
    ) -> bool:
        index, reason = self.knowledge.find_copilot_recipe_action(
            state_signature=state_signature,
            candidates=candidates,
        )
        if index is None:
            return False

        node = locator_by_id.get(index)
        if node is None:
            if state_signature and index < len(candidates):
                failed_label = str(candidates[index].get("label", "")).strip()
                if failed_label:
                    self.knowledge.mark_copilot_recipe_result(
                        state_signature=state_signature,
                        action_label=failed_label,
                        success=False,
                    )
            return False

        label = str(candidates[index].get("label", "")).strip() if index < len(candidates) else ""
        try:
            if label:
                print(f"Copilot memory click: {label} | {reason}")
            node.click()
            self._append_action_history(action_history, label or f"memory_id={index}")
            if state_signature and label:
                self.knowledge.mark_copilot_recipe_result(
                    state_signature=state_signature,
                    action_label=label,
                    success=True,
                )
            return True
        except Exception:
            if state_signature and label:
                self.knowledge.mark_copilot_recipe_result(
                    state_signature=state_signature,
                    action_label=label,
                    success=False,
                )
            return False

    @staticmethod
    def _repeated_action_tail(action_history: list[str] | None) -> int:
        if not action_history:
            return 0
        normalized = [_normalize(item) for item in action_history if str(item).strip()]
        if not normalized:
            return 0
        tail = normalized[-1]
        count = 0
        for item in reversed(normalized):
            if item != tail:
                break
            count += 1
        return count

    @staticmethod
    def _should_trigger_stuck_recovery(state_repeat_count: int, repeated_action_count: int) -> bool:
        return state_repeat_count >= 2 or repeated_action_count >= 3

    @staticmethod
    def _extract_html_excerpt(page: Page, max_chars: int = 32000) -> tuple[str, str]:
        try:
            raw_html = page.content() or ""
        except Exception:
            return "", ""
        if not raw_html:
            return "", ""

        compact = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw_html)
        compact = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", compact)
        compact = re.sub(r"\s+", " ", compact).strip()
        return raw_html, compact[:max_chars]

    def _save_stuck_html_snapshot(self, job: JobPosting, stage: str, raw_html: str) -> Path | None:
        if not raw_html.strip():
            return None
        safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "_", stage).strip("_") or "stage"
        timestamp = int(time.time())
        out_dir = self.settings.base_dir / "output" / "stuck_html"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{job.job_id}_{safe_stage}_{timestamp}.html"
        try:
            out_path.write_text(raw_html, encoding="utf-8")
            return out_path
        except Exception:
            return None

    def _try_llm_stuck_recovery(
        self,
        page: Page,
        job: JobPosting,
        stage: str,
        state_signature: str,
        root: Page | Locator,
        allow_plain_anchors: bool,
        helper: FormHelper,
        action_history: list[str],
    ) -> str:
        raw_html, html_excerpt = self._extract_html_excerpt(page)
        snapshot_path = self._save_stuck_html_snapshot(job=job, stage=stage, raw_html=raw_html)
        if snapshot_path is not None:
            print(f"[Copilot] Stuck HTML snapshot: {snapshot_path}")
        self.metrics.record_fallback_trigger(stage=stage)
        outcome = self.agentic_fallback.run(
            page=page,
            root=root,
            job=job,
            stage=stage,
            state_signature=state_signature,
            helper=helper,
            action_history=action_history,
            allow_plain_anchors=allow_plain_anchors,
        )
        if outcome.trace_path:
            print(f"[Copilot] Agentic trace: {outcome.trace_path}")
        self.metrics.record_fallback_outcome(
            stage=stage,
            result=outcome.result,
            playbook_source=outcome.playbook_source,
            tool_steps_used=outcome.tool_steps_used,
        )
        if state_signature and outcome.playbook_steps:
            if outcome.playbook_source == "memory":
                if outcome.playbook_fingerprint:
                    self.knowledge.mark_agentic_playbook_result(
                        state_signature=state_signature,
                        stage=stage,
                        step_fingerprint=outcome.playbook_fingerprint,
                        success=outcome.result in {"clicked", "retry"},
                    )
            else:
                step_fingerprint = self.knowledge.remember_agentic_playbook(
                    state_signature=state_signature,
                    stage=stage,
                    steps=outcome.playbook_steps,
                    source="llm_agentic",
                    notes=outcome.reason or "",
                )
                if step_fingerprint:
                    self.knowledge.mark_agentic_playbook_result(
                        state_signature=state_signature,
                        stage=stage,
                        step_fingerprint=step_fingerprint,
                        success=outcome.result in {"clicked", "retry"},
                    )

        if outcome.result == "clicked":
            label = outcome.clicked_label.strip()
            if label:
                print(f"LLM stuck recovery click: {label} | {outcome.reason}")
                self._append_action_history(action_history, label)
                if state_signature:
                    self.knowledge.remember_copilot_recipe(
                        state_signature=state_signature,
                        action_label=label,
                        source="llm_stuck",
                    )
            return "clicked"

        if outcome.result == "retry":
            if outcome.reason:
                print(f"LLM stuck recovery: retry | {outcome.reason}")
            return "retry"

        if outcome.result == "human":
            if outcome.reason:
                print(f"LLM stuck recovery: wait_human | {outcome.reason}")
            return "human"

        if html_excerpt:
            print("LLM stuck recovery: no safe action from agentic controller.")
        return "none"

    def _learn_from_manual_delta(
        self,
        before_snapshot: list[dict[str, Any]],
        after_snapshot: list[dict[str, Any]],
    ) -> int:
        before_values: dict[str, str] = {}
        for field in before_snapshot:
            identity = self._snapshot_field_identity(field)
            if not identity:
                continue
            value = str(field.get("current_value", "")).strip()
            before_values[identity] = value

        observations: list[dict[str, str]] = []
        for field in after_snapshot:
            label = str(field.get("label", "")).strip()
            context = str(field.get("context", "")).strip()
            value = str(field.get("current_value", "")).strip()
            if not label or not value:
                continue
            identity = self._snapshot_field_identity(field)
            if not identity:
                continue
            if before_values.get(identity, "").strip() == value:
                continue
            observations.append(
                {
                    "label": label,
                    "context": context,
                    "value": value,
                }
            )

        if not observations:
            return 0
        return self.knowledge.learn_from_observations(observations)

    def _await_human_assist(
        self,
        page: Page,
        helper: FormHelper | None,
        stage: str,
        reason: str,
        validation_messages: list[str] | None = None,
        state_signature: str = "",
        action_candidates: list[dict[str, str]] | None = None,
    ) -> str:
        if not self.settings.copilot_mode:
            return "unavailable"
        if self._is_page_closed(page):
            return "unavailable"

        before_snapshot: list[dict[str, Any]] = []
        before_fingerprint = ""
        if helper is not None:
            before_snapshot = helper.collect_visible_fields_snapshot(page)
            before_fingerprint = self._snapshot_fingerprint(before_snapshot)
        before_url = page.url
        events_cursor = self._interaction_cursor(page)
        self.metrics.record_human_handoff(stage=stage, result="requested")

        print(f"[Copilot] Manual assistance needed ({stage}): {reason}")
        if validation_messages:
            for message in validation_messages[:5]:
                print(f"  - {message}")
        print(
            "[Copilot] Perform missing step in the browser (choose suggestion, click next, solve captcha, etc.)."
        )
        print("[Copilot] Waiting for page changes. Resume is automatic; no terminal input required.")

        timeout = float(self.settings.copilot_wait_timeout_sec)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_page_closed(page):
                return "unavailable"
            page.wait_for_timeout(self.settings.copilot_poll_interval_ms)

            recent_events, events_cursor = self._interaction_events_since(page, events_cursor)
            current_url = page.url
            url_changed = current_url != before_url

            after_snapshot: list[dict[str, Any]] = []
            snapshot_changed = False
            if helper is not None:
                after_snapshot = helper.collect_visible_fields_snapshot(page)
                after_fingerprint = self._snapshot_fingerprint(after_snapshot)
                snapshot_changed = after_fingerprint != before_fingerprint

            validation_changed = False
            if validation_messages is not None:
                current_validation = self._collect_validation_messages(page)
                validation_changed = current_validation[:6] != validation_messages[:6]

            if not (recent_events or url_changed or snapshot_changed or validation_changed):
                continue

            if helper is not None:
                learned = self._learn_from_manual_delta(before_snapshot, after_snapshot)
                if learned > 0:
                    print(f"[Copilot] Learned {learned} field answers from manual intervention.")

            if state_signature and action_candidates and recent_events:
                self._learn_recipe_from_human_events(
                    state_signature=state_signature,
                    action_candidates=action_candidates,
                    events=recent_events,
                )
                self._learn_agentic_playbook_from_human_events(
                    state_signature=state_signature,
                    stage=stage,
                    action_candidates=action_candidates,
                    events=recent_events,
                )
            return "resume"

        print("[Copilot] Manual assistance timeout reached.")
        if self.settings.copilot_auto_skip_on_timeout:
            return "skip"
        return "unavailable"

    @staticmethod
    def _collect_validation_messages(page: Page) -> list[str]:
        selectors = (
            "[role='alert']",
            ".error-message",
            ".invalid-feedback",
            ".field-error",
            ".form-error",
            ".artdeco-inline-feedback__message",
            ".fb-form-element__error",
            "[aria-invalid='true']",
        )
        messages: list[str] = []
        for selector in selectors:
            locator = page.locator(selector)
            try:
                max_scan = min(locator.count(), 40)
            except Exception:
                continue
            for idx in range(max_scan):
                node = locator.nth(idx)
                try:
                    if not node.is_visible():
                        continue
                    text = " ".join((node.inner_text() or "").split()).strip()
                except Exception:
                    continue
                if not text:
                    continue
                if text not in messages:
                    messages.append(text[:220])
                if len(messages) >= 12:
                    return messages
        return messages

    def _collect_page_signals(
        self,
        page: Page,
        visible_fields: list[dict],
        validation_messages: list[str],
    ) -> dict[str, str | int | bool | list[str]]:
        required_unanswered: list[str] = []
        for field in visible_fields:
            label = str(field.get("label", "")).strip()
            if not label or not bool(field.get("required", False)):
                continue

            kind = str(field.get("kind", "")).strip().lower()
            current_value = str(field.get("current_value", "")).strip()
            if kind in {"text", "select", "combobox", "radio"} and not current_value:
                required_unanswered.append(label[:160])

        has_hcaptcha = False
        has_recaptcha = False
        try:
            has_hcaptcha = page.locator("iframe[src*='hcaptcha'], .h-captcha, #h-captcha, [data-sitekey]").count() > 0
        except Exception:
            has_hcaptcha = False
        try:
            has_recaptcha = page.locator("iframe[src*='recaptcha'], .g-recaptcha, [data-recaptcha]").count() > 0
        except Exception:
            has_recaptcha = False

        return {
            "required_unanswered": required_unanswered[:12],
            "required_unanswered_count": len(required_unanswered),
            "validation_messages": validation_messages[:12],
            "has_hcaptcha": has_hcaptcha,
            "has_recaptcha": has_recaptcha,
            "requires_external_login": self._requires_external_login(page),
            "url": page.url[:260],
        }
