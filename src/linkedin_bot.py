from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright
from playwright_stealth import Stealth

from src.config import Settings
from src.cv_tools import write_cover_letter, write_tailored_cv_notes
from src.form_helper import FormHelper
from src.knowledge_store import KnowledgeStore
from src.llm_agent import LLMJobAgent
from src.models import ApplicationRecord, JobPosting


def _normalize(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", "", lowered)
    return lowered


class LinkedInJobApplier:
    SAVED_JOBS_URL = "https://www.linkedin.com/my-items/saved-jobs/"
    FEED_URL = "https://www.linkedin.com/feed/"

    EASY_APPLY_TOKENS = ("easy apply", "latwe aplikowanie")
    EXTERNAL_APPLY_TOKENS = ("apply", "aplikuj", "visit", "apply on company")
    NEXT_TOKENS = ("next", "dalej", "continue", "kontynuuj")
    REVIEW_TOKENS = ("review", "przejrzyj")
    SUBMIT_TOKENS = ("submit application", "wyslij aplikacje", "submit", "aplikuj")
    CLOSE_TOKENS = ("dismiss", "close", "cancel", "anuluj", "zamknij")
    DISCARD_TOKENS = ("discard", "odrzuc", "leave", "wyjdz")

    def __init__(
        self,
        settings: Settings,
        knowledge: KnowledgeStore,
        llm: LLMJobAgent,
        cv_text: str,
        cv_path: Path,
    ):
        self.settings = settings
        self.knowledge = knowledge
        self.llm = llm
        self.cv_text = cv_text
        self.cv_path = cv_path
        self.stealth = Stealth()

    def run(self) -> None:
        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._stealth_page(page)
                self._ensure_logged_in(page)

                job_urls = self._get_saved_job_urls(page)
                if not job_urls:
                    print("No saved jobs found.")
                    return

                print(f"Saved jobs detected: {len(job_urls)}")
                processed = 0
                for job_url in job_urls[: self.settings.max_jobs_per_run]:
                    processed += 1
                    print(f"\n[{processed}] Processing: {job_url}")
                    self._process_single_job(page, context, job_url)
            finally:
                context.close()

    def _launch_context(self, playwright: Playwright) -> BrowserContext:
        self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.settings.browser_profile_dir),
            headless=False,
            slow_mo=self.settings.slow_mo_ms,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1400, "height": 1000},
        )
        try:
            self.stealth.apply_stealth_sync(context)
        except Exception:
            pass
        for page in context.pages:
            self._stealth_page(page)
        context.on("page", self._stealth_page)
        return context

    def _stealth_page(self, page: Page) -> None:
        try:
            self.stealth.apply_stealth_sync(page)
        except Exception:
            return

    def _ensure_logged_in(self, page: Page) -> None:
        page.goto(self.FEED_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        current_url = page.url.lower()
        on_login_page = "linkedin.com/login" in current_url or "checkpoint" in current_url
        has_login_inputs = page.locator("input[name='session_key'], input#username").count() > 0
        if on_login_page or has_login_inputs:
            print("Please login to LinkedIn in the opened browser window, then press Enter here.")
            input()
            page.goto(self.FEED_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)

    def _get_saved_job_urls(self, page: Page) -> list[str]:
        page.goto(self.SAVED_JOBS_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
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

    def _process_single_job(self, page: Page, context: BrowserContext, job_url: str) -> None:
        if self.settings.skip_already_applied and self.knowledge.was_already_applied(job_url):
            print("Skipped: already processed before.")
            return

        job = self._read_job_posting(page, job_url)
        if not job.description.strip():
            print("Skipped: missing job description.")
            return

        decision = self.llm.analyze_job(
            job=job,
            cv_text=self.cv_text,
            profile=self.knowledge.profile,
            known_answers=self.knowledge.field_answers,
        )
        print(f"Fit score: {decision.fit_score}/100 | should_apply={decision.should_apply}")
        if decision.reasoning:
            print(f"Reason: {decision.reasoning}")

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
        else:
            print("Skipped: unsupported apply mode.")

        status = "submitted" if submitted else "not_submitted"
        self.knowledge.save_application(
            ApplicationRecord(
                url=job.url,
                title=job.title,
                company=job.company,
                status=status,
                notes=f"mode={job.apply_mode}",
            )
        )
        print(f"Application result: {status}")

    def _read_job_posting(self, page: Page, job_url: str) -> JobPosting:
        page.goto(job_url, wait_until="domcontentloaded")
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

    def _detect_apply_mode(self, page: Page) -> str:
        easy = self._find_action(page, self.EASY_APPLY_TOKENS, ())
        if easy:
            return "easy"
        external = self._find_action(page, self.EXTERNAL_APPLY_TOKENS, self.EASY_APPLY_TOKENS)
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
        trigger = self._find_action(page, self.EASY_APPLY_TOKENS, ())
        if not trigger:
            print("Easy Apply button not found.")
            return False

        try:
            trigger.click()
        except Exception:
            return False
        page.wait_for_timeout(1200)

        helper = FormHelper(
            knowledge=self.knowledge,
            cv_path=self.cv_path,
            cover_letter_path=cover_letter_path,
            prefilled_answers=prefilled_answers,
            ai_disclosure_text=self.settings.ai_disclosure_text if self.settings.ai_disclosure_enabled else "",
        )

        for _ in range(12):
            helper.fill_visible_fields(page)

            if self._click_action_with_confirmation(page, self.SUBMIT_TOKENS, job):
                page.wait_for_timeout(1800)
                if self._easy_apply_success_detected(page):
                    self._close_any_dialog(page)
                    return True

            if self._click_action(page, self.REVIEW_TOKENS, ()):
                page.wait_for_timeout(1000)
                continue
            if self._click_action(page, self.NEXT_TOKENS, ()):
                page.wait_for_timeout(1000)
                continue
            break

        self._close_any_dialog(page)
        return False

    def _apply_external(
        self,
        context: BrowserContext,
        page: Page,
        job: JobPosting,
        cover_letter_path: Path | None,
        prefilled_answers: dict[str, str],
    ) -> bool:
        trigger = self._find_action(page, self.EXTERNAL_APPLY_TOKENS, self.EASY_APPLY_TOKENS)
        if not trigger:
            print("External apply button not found.")
            return False

        external_page: Page | None = None
        try:
            with context.expect_page(timeout=6000) as event_info:
                trigger.click()
            external_page = event_info.value
            external_page.wait_for_load_state("domcontentloaded")
        except PlaywrightTimeoutError:
            previous_url = page.url
            try:
                trigger.click()
            except Exception:
                return False
            page.wait_for_timeout(2000)
            if page.url != previous_url:
                external_page = page
        except Exception:
            return False

        if external_page is None:
            return False

        self._stealth_page(external_page)
        helper = FormHelper(
            knowledge=self.knowledge,
            cv_path=self.cv_path,
            cover_letter_path=cover_letter_path,
            prefilled_answers=prefilled_answers,
            ai_disclosure_text=self.settings.ai_disclosure_text if self.settings.ai_disclosure_enabled else "",
        )

        submitted = False
        for _ in range(8):
            helper.fill_visible_fields(external_page)

            if self._click_action_with_confirmation(external_page, self.SUBMIT_TOKENS, job):
                submitted = True
                external_page.wait_for_timeout(1200)
                break
            if self._click_action(external_page, self.NEXT_TOKENS, ()):
                external_page.wait_for_timeout(900)
                continue
            break

        try:
            if external_page is not page:
                external_page.close()
        except Exception:
            pass
        return submitted

    def _click_action_with_confirmation(self, root: Page | Locator, tokens: tuple[str, ...], job: JobPosting) -> bool:
        action = self._find_action(root, tokens, ())
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
        action = self._find_action(root, tokens, exclude_tokens)
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
    ) -> Locator | None:
        lookup_tokens = tuple(_normalize(token) for token in tokens)
        lookup_exclusions = tuple(_normalize(token) for token in exclude_tokens)
        candidates = root.locator("button, a[role='button'], a")
        max_scan = min(candidates.count(), 300)

        for index in range(max_scan):
            node = candidates.nth(index)
            try:
                if not node.is_visible():
                    continue
            except Exception:
                continue

            try:
                label = (node.inner_text() or "").strip()
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

    def _close_any_dialog(self, page: Page) -> None:
        if self._click_action(page, self.CLOSE_TOKENS, ()):
            page.wait_for_timeout(600)
        self._click_action(page, self.DISCARD_TOKENS, ())

    def _can_submit(self, job: JobPosting) -> bool:
        if self.settings.auto_submit:
            return True
        answer = input(f"Submit application for {job.title} at {job.company}? [y/N]: ").strip().lower()
        return answer in {"y", "yes", "tak", "t"}

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
