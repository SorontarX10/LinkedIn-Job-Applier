from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Page


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 +#./-]+", "", text)
    return text


@dataclass(frozen=True)
class DiscoveryQuery:
    keywords: str
    location: str = ""
    easy_apply_only: bool = False
    remote_only: bool = False
    days_back: int = 30
    source: str = "profile_cv"


@dataclass
class DiscoveryJob:
    job_id: str
    url: str
    title: str
    company: str
    location: str
    snippet: str
    source_query: str


class JobDiscovery:
    SEARCH_BASE_URL = "https://www.linkedin.com/jobs/search/"
    CACHE_VERSION = 1

    def __init__(
        self,
        cache_path: Path | None = None,
        cache_ttl_minutes: int = 90,
    ) -> None:
        self.cache_path = cache_path
        self.cache_ttl_minutes = max(1, int(cache_ttl_minutes))

    def build_queries(
        self,
        *,
        profile: dict[str, str],
        cv_text: str,
        include_keywords: list[str] | None = None,
        exclude_keywords: list[str] | None = None,
        locations: list[str] | None = None,
        easy_apply_only: bool = False,
        remote_only: bool = False,
        days_back: int = 30,
        max_queries: int = 12,
    ) -> list[DiscoveryQuery]:
        include = [str(item).strip() for item in (include_keywords or []) if str(item).strip()]
        exclude = [_normalize(item) for item in (exclude_keywords or []) if str(item).strip()]

        seed_phrases = include + self._extract_profile_seed_phrases(profile) + self._extract_cv_seed_phrases(cv_text)
        seen_phrases: set[str] = set()
        filtered_phrases: list[str] = []
        for phrase in seed_phrases:
            clean = re.sub(r"\s+", " ", phrase).strip()
            if not clean:
                continue
            norm = _normalize(clean)
            if not norm or norm in seen_phrases:
                continue
            if any(ex in norm for ex in exclude):
                continue
            seen_phrases.add(norm)
            filtered_phrases.append(clean[:120])
            if len(filtered_phrases) >= max(1, max_queries * 2):
                break

        if not filtered_phrases:
            filtered_phrases = ["seo ai automation"]

        location_candidates = [str(item).strip() for item in (locations or []) if str(item).strip()]
        if not location_candidates:
            city = str(profile.get("city", "")).strip()
            country = str(profile.get("country", "")).strip()
            if city and country:
                location_candidates.append(f"{city}, {country}")
            elif country:
                location_candidates.append(country)
        if not location_candidates:
            location_candidates = [""]

        queries: list[DiscoveryQuery] = []
        seen_query_keys: set[str] = set()
        for phrase in filtered_phrases:
            for location in location_candidates:
                query = DiscoveryQuery(
                    keywords=phrase,
                    location=location,
                    easy_apply_only=bool(easy_apply_only),
                    remote_only=bool(remote_only),
                    days_back=max(1, int(days_back)),
                    source="profile_cv",
                )
                key = f"{_normalize(query.keywords)}|{_normalize(query.location)}|{int(query.easy_apply_only)}|{int(query.remote_only)}|{query.days_back}"
                if key in seen_query_keys:
                    continue
                seen_query_keys.add(key)
                queries.append(query)
                if len(queries) >= max(1, int(max_queries)):
                    return queries
        return queries

    def build_search_url(self, query: DiscoveryQuery, start: int = 0) -> str:
        params: dict[str, str] = {
            "keywords": query.keywords,
            "start": str(max(0, int(start))),
        }
        if query.location:
            params["location"] = query.location
        if query.easy_apply_only:
            params["f_AL"] = "true"
        if query.remote_only:
            # LinkedIn working type: 2 = remote (best-effort, may change over time).
            params["f_WT"] = "2"
        if query.days_back > 0:
            seconds = max(1, int(query.days_back)) * 24 * 60 * 60
            params["f_TPR"] = f"r{seconds}"
        return f"{self.SEARCH_BASE_URL}?{urlencode(params)}"

    def discover_jobs(
        self,
        *,
        page: Page,
        queries: list[DiscoveryQuery],
        max_results: int = 120,
        pages_per_query: int = 2,
        scroll_iterations: int = 3,
        scroll_px: int = 2600,
    ) -> list[DiscoveryJob]:
        if self._is_page_closed(page):
            return []

        cache_fingerprint = self._build_cache_fingerprint(
            queries=queries,
            max_results=max_results,
            pages_per_query=pages_per_query,
            scroll_iterations=scroll_iterations,
            scroll_px=scroll_px,
        )
        cached = self._load_cached_results(cache_fingerprint=cache_fingerprint)
        if cached:
            return cached

        results: list[DiscoveryJob] = []
        seen_urls: set[str] = set()
        query_pages = max(1, int(pages_per_query))
        result_limit = max(1, int(max_results))

        for query in queries:
            for page_index in range(query_pages):
                if self._is_page_closed(page):
                    return results
                start = page_index * 25
                search_url = self.build_search_url(query, start=start)
                try:
                    page.goto(search_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1200)
                except Exception:
                    continue

                self._scroll(page, iterations=scroll_iterations, pixels=scroll_px)
                cards = self._extract_search_cards(page)

                for card in cards:
                    raw_href = str(card.get("href", "")).strip()
                    normalized_url = self._normalize_job_url(raw_href)
                    if not normalized_url or normalized_url in seen_urls:
                        continue
                    seen_urls.add(normalized_url)

                    title = self._clean_text(str(card.get("title", "")).strip()) or "Unknown title"
                    company = self._clean_text(str(card.get("company", "")).strip()) or "Unknown company"
                    location = self._clean_text(str(card.get("location", "")).strip())
                    snippet = self._clean_text(str(card.get("snippet", "")).strip())[:600]
                    results.append(
                        DiscoveryJob(
                            job_id=self._extract_job_id(normalized_url),
                            url=normalized_url,
                            title=title,
                            company=company,
                            location=location,
                            snippet=snippet,
                            source_query=query.keywords[:120],
                        )
                    )
                    if len(results) >= result_limit:
                        self._save_cached_results(
                            cache_fingerprint=cache_fingerprint,
                            queries=queries,
                            jobs=results,
                        )
                        return results
        self._save_cached_results(
            cache_fingerprint=cache_fingerprint,
            queries=queries,
            jobs=results,
        )
        return results

    def _build_cache_fingerprint(
        self,
        *,
        queries: list[DiscoveryQuery],
        max_results: int,
        pages_per_query: int,
        scroll_iterations: int,
        scroll_px: int,
    ) -> str:
        payload = {
            "version": self.CACHE_VERSION,
            "max_results": int(max_results),
            "pages_per_query": int(pages_per_query),
            "scroll_iterations": int(scroll_iterations),
            "scroll_px": int(scroll_px),
            "queries": [
                {
                    "keywords": query.keywords,
                    "location": query.location,
                    "easy_apply_only": bool(query.easy_apply_only),
                    "remote_only": bool(query.remote_only),
                    "days_back": int(query.days_back),
                    "source": query.source,
                }
                for query in queries
            ],
        }
        compact = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(compact.encode("utf-8")).hexdigest()

    def _load_cached_results(self, cache_fingerprint: str) -> list[DiscoveryJob]:
        if self.cache_path is None or not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []
        if int(payload.get("version", 0)) != self.CACHE_VERSION:
            return []
        if str(payload.get("query_fingerprint", "")).strip() != cache_fingerprint:
            return []
        generated_at_raw = str(payload.get("generated_at_utc", "")).strip()
        if not generated_at_raw:
            return []
        generated_at = self._parse_utc(generated_at_raw)
        if generated_at is None:
            return []
        ttl_deadline = generated_at + timedelta(minutes=self.cache_ttl_minutes)
        if datetime.now(timezone.utc) > ttl_deadline:
            return []
        cached_jobs = payload.get("jobs")
        if not isinstance(cached_jobs, list):
            return []
        results: list[DiscoveryJob] = []
        for item in cached_jobs:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            results.append(
                DiscoveryJob(
                    job_id=str(item.get("job_id", "")).strip() or self._extract_job_id(url),
                    url=url,
                    title=str(item.get("title", "")).strip() or "Unknown title",
                    company=str(item.get("company", "")).strip() or "Unknown company",
                    location=str(item.get("location", "")).strip(),
                    snippet=str(item.get("snippet", "")).strip()[:600],
                    source_query=str(item.get("source_query", "")).strip()[:120],
                )
            )
        return results

    def _save_cached_results(
        self,
        *,
        cache_fingerprint: str,
        queries: list[DiscoveryQuery],
        jobs: list[DiscoveryJob],
    ) -> None:
        if self.cache_path is None:
            return
        payload = {
            "version": self.CACHE_VERSION,
            "query_fingerprint": cache_fingerprint,
            "generated_at_utc": self._utc_now(),
            "cache_ttl_minutes": self.cache_ttl_minutes,
            "query_count": len(queries),
            "result_count": len(jobs),
            "queries": [
                {
                    "keywords": query.keywords,
                    "location": query.location,
                    "easy_apply_only": bool(query.easy_apply_only),
                    "remote_only": bool(query.remote_only),
                    "days_back": int(query.days_back),
                    "source": query.source,
                }
                for query in queries
            ],
            "jobs": [
                {
                    "job_id": job.job_id,
                    "url": job.url,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "snippet": job.snippet,
                    "source_query": job.source_query,
                }
                for job in jobs
            ],
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return

    def _extract_profile_seed_phrases(self, profile: dict[str, str]) -> list[str]:
        fields = (
            "current_title",
            "core_skills",
            "professional_summary",
        )
        phrases: list[str] = []
        for key in fields:
            value = str(profile.get(key, "")).strip()
            if not value:
                continue
            parts = re.split(r"[|;/,\n]+", value)
            for part in parts:
                clean = self._clean_text(part)
                if not clean:
                    continue
                words = clean.split()
                if 1 <= len(words) <= 10:
                    phrases.append(clean)
        return phrases

    def _extract_cv_seed_phrases(self, cv_text: str) -> list[str]:
        if not cv_text.strip():
            return []

        seed_tokens = (
            "ai",
            "agentic",
            "llm",
            "openai",
            "python",
            "automation",
            "seo",
            "product",
            "manager",
            "advisor",
            "analytics",
            "machine learning",
            "data",
            "growth",
            "technical seo",
        )
        phrases: list[str] = []
        for raw_line in cv_text.splitlines()[:240]:
            line = self._clean_text(raw_line)
            if len(line) < 8 or len(line) > 120:
                continue
            norm = _normalize(line)
            if not any(token in norm for token in seed_tokens):
                continue
            phrases.append(line)
            if len(phrases) >= 12:
                break
        return phrases

    @staticmethod
    def _scroll(page: Page, iterations: int, pixels: int) -> None:
        for _ in range(max(1, int(iterations))):
            if JobDiscovery._is_page_closed(page):
                return
            try:
                page.mouse.wheel(0, int(pixels))
                page.wait_for_timeout(500)
            except Exception:
                return

    @staticmethod
    def _extract_search_cards(page: Page) -> list[dict[str, Any]]:
        try:
            raw = page.evaluate(
                """() => {
                    const anchors = Array.from(document.querySelectorAll("a[href*='/jobs/view/']"));
                    const out = [];
                    const seen = new Set();
                    const pickText = (root, selectors) => {
                        if (!root) return '';
                        for (const selector of selectors) {
                            const el = root.querySelector(selector);
                            if (!el) continue;
                            const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (text) return text;
                        }
                        return '';
                    };
                    for (const anchor of anchors) {
                        const href = anchor.getAttribute('href') || '';
                        if (!href.includes('/jobs/view/')) continue;
                        const card = anchor.closest('li') || anchor.closest("div[class*='job']");
                        const title = (
                            (anchor.textContent || '').replace(/\\s+/g, ' ').trim()
                            || pickText(card, ['h3', '[class*=title]', '[data-test-job-card-title]'])
                        );
                        const company = pickText(card, [
                            '.base-search-card__subtitle',
                            '.job-card-container__company-name',
                            '[class*=company]',
                        ]);
                        const location = pickText(card, [
                            '.job-search-card__location',
                            '.base-search-card__metadata',
                            '[class*=location]',
                        ]);
                        const snippet = pickText(card, [
                            '.job-search-card__snippet',
                            '[class*=description]',
                            '[class*=snippet]',
                        ]);
                        const key = `${href}|${title}|${company}`;
                        if (seen.has(key)) continue;
                        seen.add(key);
                        out.push({ href, title, company, location, snippet });
                    }
                    return out.slice(0, 500);
                }"""
            )
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        items: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict):
                items.append(item)
        return items

    @staticmethod
    def _normalize_job_url(href: str) -> str:
        absolute = urljoin("https://www.linkedin.com", href).split("?")[0].strip()
        if "/jobs/view/" not in absolute:
            return ""
        absolute = re.sub(r"/+$", "", absolute)
        return f"{absolute}/"

    @staticmethod
    def _extract_job_id(job_url: str) -> str:
        match = re.search(r"/jobs/view/(\d+)", job_url)
        if match:
            return match.group(1)
        digest = hashlib.sha1(job_url.encode("utf-8")).hexdigest()
        return digest[:12]

    @staticmethod
    def _clean_text(text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        return compact[:220]

    @staticmethod
    def _is_page_closed(page: Page) -> bool:
        try:
            return page.is_closed()
        except Exception:
            return True

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _parse_utc(raw_value: str) -> datetime | None:
        candidate = raw_value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
