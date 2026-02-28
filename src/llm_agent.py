from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from openai import OpenAI

from src.models import DiscoveryScore, FitDecision, JobPosting


class LLMJobAgent:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.enabled = bool(api_key.strip())
        self.client = OpenAI(api_key=api_key) if self.enabled else None
        self._candidate_knowledge_cache_key = ""
        self._candidate_knowledge_cache: dict[str, Any] = {}

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
                "requires_work_outside_poland": "boolean, true only if the role explicitly requires being based outside Poland",
                "location_restriction_reasoning": "short string explaining location requirement",
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
                requires_work_outside_poland=bool(data.get("requires_work_outside_poland", False)),
                location_restriction_reasoning=str(data.get("location_restriction_reasoning", "")).strip(),
                missing_information=self._safe_list(data.get("missing_information")),
                tailored_cv_notes=self._safe_list(data.get("tailored_cv_notes")),
                cover_letter=str(data.get("cover_letter", "")).strip(),
                prefilled_answers=self._safe_dict(data.get("prefilled_answers")),
            )
        except Exception as exc:
            fallback = self._heuristic_decision(job)
            fallback.reasoning = f"{fallback.reasoning} LLM error: {exc}"
            return fallback

    def score_discovery_job(
        self,
        job: Any,
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
        preferences: dict[str, Any] | None = None,
    ) -> DiscoveryScore:
        job_payload = self._coerce_discovery_job_payload(job)
        heuristic = self._heuristic_discovery_score(
            job_payload=job_payload,
            profile=profile,
            known_answers=known_answers,
            cv_text=cv_text,
            preferences=preferences,
        )
        if not self.enabled:
            return heuristic

        candidate_knowledge_json = self._build_candidate_knowledge_json(
            profile=profile,
            known_answers=known_answers,
            cv_text=cv_text,
        )
        system_prompt = (
            "You are a strict discovery-ranking assistant for job search.\n"
            "Score each job using only provided candidate data and job snippet.\n"
            "Never invent facts and never guess missing requirements.\n"
            "Prefer realistic scoring over optimistic assumptions.\n"
            "Return JSON only."
        )
        user_payload = {
            "job": job_payload,
            "candidate_knowledge_json": candidate_knowledge_json,
            "preferences": preferences or {},
            "scoring_dimensions": [
                "skill_match_score",
                "experience_match_score",
                "constraint_score",
                "applyability_score",
            ],
            "rules": {
                "score_range": "0-100 integers",
                "priority_formula": "priority_score = 0.40*skill_match_score + 0.25*experience_match_score + 0.20*constraint_score + 0.15*applyability_score",
                "hard_reject": "true only for strong explicit mismatch (e.g. hard location/authorization mismatch from given evidence)",
            },
            "output_format": {
                "skill_match_score": "integer 0-100",
                "experience_match_score": "integer 0-100",
                "constraint_score": "integer 0-100",
                "applyability_score": "integer 0-100",
                "priority_score": "integer 0-100",
                "hard_reject": "boolean",
                "reject_reason": "short string",
                "reasoning": "short string",
            },
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)

            skill = self._safe_int(data.get("skill_match_score", heuristic.skill_match_score))
            experience = self._safe_int(data.get("experience_match_score", heuristic.experience_match_score))
            constraint = self._safe_int(data.get("constraint_score", heuristic.constraint_score))
            applyability = self._safe_int(data.get("applyability_score", heuristic.applyability_score))
            priority_raw = data.get("priority_score")
            if priority_raw is None:
                priority = self._compute_discovery_priority(skill, experience, constraint, applyability)
            else:
                priority = self._safe_int(priority_raw)
            hard_reject = bool(data.get("hard_reject", False))
            reject_reason = str(data.get("reject_reason", "")).strip()
            reasoning = str(data.get("reasoning", "")).strip()

            if hard_reject:
                priority = min(priority, 20)

            return DiscoveryScore(
                skill_match_score=skill,
                experience_match_score=experience,
                constraint_score=constraint,
                applyability_score=applyability,
                priority_score=priority,
                hard_reject=hard_reject,
                reject_reason=reject_reason[:260],
                reasoning=reasoning[:420],
            )
        except Exception as exc:
            heuristic.reasoning = f"{heuristic.reasoning} LLM error: {exc}"
            return heuristic

    def rank_discovery_jobs(
        self,
        jobs: list[Any],
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
        preferences: dict[str, Any] | None = None,
    ) -> list[tuple[Any, DiscoveryScore]]:
        scored: list[tuple[Any, DiscoveryScore]] = []
        for job in jobs:
            score = self.score_discovery_job(
                job=job,
                profile=profile,
                known_answers=known_answers,
                cv_text=cv_text,
                preferences=preferences,
            )
            scored.append((job, score))

        scored.sort(
            key=lambda item: (
                0 if item[1].hard_reject else 1,
                item[1].priority_score,
                item[1].skill_match_score,
            ),
            reverse=True,
        )
        return scored

    def choose_action_button(
        self,
        job: JobPosting,
        page_url: str,
        stage: str,
        candidates: list[dict[str, str]],
        visible_fields: list[dict[str, Any]] | None = None,
        validation_messages: list[str] | None = None,
        page_signals: dict[str, Any] | None = None,
        recent_actions: list[str] | None = None,
    ) -> tuple[int | None, str]:
        if not self.enabled or not candidates:
            return None, ""

        system_prompt = (
            "You choose the next UI action for a job application flow.\n"
            "Choose exactly one candidate button/link id that best progresses toward submitting the application.\n"
            "If required fields are missing or validation errors are visible, prefer actions that help complete form fields.\n"
            "Avoid repeatedly clicking submit/review when validation errors still exist.\n"
            "Avoid close, cancel, discard, back, sign-in, register, policy, or navigation-away actions.\n"
            "If nothing is safe/useful, return -1.\n"
            "Return JSON only."
        )
        user_payload = {
            "job": {"title": job.title, "company": job.company, "url": job.url},
            "page": {"url": page_url, "stage": stage},
            "visible_fields": (visible_fields or [])[:60],
            "validation_messages": (validation_messages or [])[:12],
            "page_signals": page_signals or {},
            "recent_actions": (recent_actions or [])[-8:],
            "candidates": candidates[:40],
            "output_format": {"button_id": "integer id from candidates or -1", "reason": "short string"},
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)
            chosen = int(data.get("button_id", -1))
            reason = str(data.get("reason", "")).strip()
        except Exception:
            return None, ""

        if chosen < 0:
            return None, reason
        valid_ids = {int(item.get("id", -1)) for item in candidates}
        if chosen not in valid_ids:
            return None, reason
        return chosen, reason

    def choose_stuck_strategy(
        self,
        job: JobPosting,
        page_url: str,
        stage: str,
        candidates: list[dict[str, str]],
        visible_fields: list[dict[str, Any]] | None = None,
        validation_messages: list[str] | None = None,
        page_signals: dict[str, Any] | None = None,
        recent_actions: list[str] | None = None,
        html_excerpt: str = "",
    ) -> tuple[str, int | None, str]:
        allowed_strategies = {"click_candidate", "wait_human", "wait_and_retry", "fill_and_retry"}

        if not self.enabled:
            return self._heuristic_stuck_strategy(
                candidates=candidates,
                page_signals=page_signals or {},
                validation_messages=validation_messages or [],
                recent_actions=recent_actions or [],
            )

        system_prompt = (
            "You are a recovery controller for stuck web form automation.\n"
            "Given page state and HTML excerpt, choose a single strategy to break deadlock.\n"
            "Strategies:\n"
            "- click_candidate: click one candidate id now\n"
            "- fill_and_retry: refill fields and retry loop\n"
            "- wait_and_retry: do not click now, short wait and retry loop\n"
            "- wait_human: ask for human intervention (captcha, login, unclear flow)\n"
            "Use wait_human for captcha/identity checks or unclear high-risk actions.\n"
            "Avoid close/cancel/discard/sign-in/register/privacy actions unless explicitly needed.\n"
            "Return JSON only."
        )
        user_payload = {
            "job": {"title": job.title, "company": job.company, "url": job.url},
            "page": {"url": page_url, "stage": stage},
            "visible_fields": (visible_fields or [])[:60],
            "validation_messages": (validation_messages or [])[:12],
            "page_signals": page_signals or {},
            "recent_actions": (recent_actions or [])[-10:],
            "candidates": candidates[:40],
            "html_excerpt": html_excerpt[:32000],
            "output_format": {
                "strategy": "click_candidate | wait_human | wait_and_retry | fill_and_retry",
                "button_id": "integer candidate id if strategy=click_candidate, else -1",
                "reason": "short string",
            },
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)
            strategy = str(data.get("strategy", "")).strip().lower()
            reason = str(data.get("reason", "")).strip()
            button_id = int(data.get("button_id", -1))
        except Exception:
            return self._heuristic_stuck_strategy(
                candidates=candidates,
                page_signals=page_signals or {},
                validation_messages=validation_messages or [],
                recent_actions=recent_actions or [],
            )

        if strategy not in allowed_strategies:
            return self._heuristic_stuck_strategy(
                candidates=candidates,
                page_signals=page_signals or {},
                validation_messages=validation_messages or [],
                recent_actions=recent_actions or [],
            )

        if strategy != "click_candidate":
            return strategy, None, reason

        valid_ids = {int(item.get("id", -1)) for item in candidates}
        if button_id in valid_ids:
            return strategy, button_id, reason

        return self._heuristic_stuck_strategy(
            candidates=candidates,
            page_signals=page_signals or {},
            validation_messages=validation_messages or [],
            recent_actions=recent_actions or [],
        )

    def plan_stuck_tool_sequence(
        self,
        job: JobPosting,
        page_url: str,
        stage: str,
        candidates: list[dict[str, str]],
        visible_fields: list[dict[str, Any]] | None = None,
        validation_messages: list[str] | None = None,
        page_signals: dict[str, Any] | None = None,
        recent_actions: list[str] | None = None,
        html_excerpt: str = "",
        max_steps: int = 4,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        if not self.enabled:
            return [], "", False

        allowed_tools = {
            "click_action",
            "type_into_field",
            "select_option",
            "set_checkbox",
            "set_file_input",
            "wait",
            "scroll",
            "fill_visible_fields",
            "human_handoff",
        }
        steps_cap = max(1, min(10, int(max_steps)))

        system_prompt = (
            "You are a tool-planning controller for stuck form automation.\n"
            "Return a short sequence of tool calls to unblock progress.\n"
            "Use only allowed tools and only IDs/labels provided in candidates/visible_fields.\n"
            "Prioritize truthful form completion and safe progression.\n"
            "If captcha/login or high uncertainty, choose human handoff.\n"
            "Avoid close/cancel/discard/delete/logout actions.\n"
            "Return JSON only."
        )
        user_payload = {
            "job": {"title": job.title, "company": job.company, "url": job.url},
            "page": {"url": page_url, "stage": stage},
            "candidates": candidates[:40],
            "visible_fields": (visible_fields or [])[:80],
            "validation_messages": (validation_messages or [])[:12],
            "page_signals": page_signals or {},
            "recent_actions": (recent_actions or [])[-10:],
            "html_excerpt": html_excerpt[:32000],
            "allowed_tools": sorted(allowed_tools),
            "max_steps": steps_cap,
            "output_format": {
                "decision": "execute | wait_human | fallback",
                "reason": "short string",
                "steps": [
                    {
                        "tool": "allowed tool name",
                        "payload": {"tool specific payload": "value"},
                    }
                ],
            },
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)
        except Exception:
            return [], "", False

        decision = str(data.get("decision", "")).strip().lower()
        reason = str(data.get("reason", "")).strip()[:240]
        if decision == "wait_human":
            return [], reason, True
        if decision != "execute":
            return [], reason, False

        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            return [], reason, False

        planned_steps: list[dict[str, Any]] = []
        for step in raw_steps[:steps_cap]:
            if not isinstance(step, dict):
                continue
            tool = str(step.get("tool", "")).strip().lower()
            if tool not in allowed_tools:
                continue
            payload = step.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            sanitized_payload: dict[str, Any] = {}
            for key, value in payload.items():
                key_text = str(key).strip()[:80]
                if not key_text:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    sanitized_payload[key_text] = value
                else:
                    sanitized_payload[key_text] = str(value)[:220]
            planned_steps.append(
                {
                    "tool": tool,
                    "payload": sanitized_payload,
                }
            )
        return planned_steps, reason, False

    def propose_form_answers(
        self,
        job: JobPosting,
        page_url: str,
        stage: str,
        visible_fields: list[dict[str, Any]],
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
        validation_messages: list[str] | None = None,
        page_signals: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if not self.enabled or not visible_fields:
            return {}

        candidate_knowledge_json = self._build_candidate_knowledge_json(
            profile=profile,
            known_answers=known_answers,
            cv_text=cv_text,
        )

        system_prompt = (
            "You fill job application form fields using only truthful candidate data.\n"
            "Primary source is candidate_knowledge_json (synthesized from CV + profile + known answers).\n"
            "Use all relevant facts from that JSON; do not fixate on any single technology keyword.\n"
            "Detect the language of each field label/context and answer in that same language.\n"
            "For experience questions, explain concretely how the candidate has used tools/approaches in practice.\n"
            "Prioritize required/invalid fields and fields mentioned by validation errors.\n"
            "For dropdown/radio fields, answer with one of provided options when possible.\n"
            "Never invent facts and do not default to generic 'no experience' statements.\n"
            "If a field cannot be answered truthfully, omit it.\n"
            "Return JSON only."
        )
        user_payload = {
            "job": {"title": job.title, "company": job.company, "url": job.url},
            "page": {"url": page_url, "stage": stage},
            "visible_fields": visible_fields[:60],
            "validation_messages": (validation_messages or [])[:12],
            "page_signals": page_signals or {},
            "candidate_knowledge_json": candidate_knowledge_json,
            "candidate_context": {
                "profile": profile,
                "known_answers": known_answers,
                "cv_text": cv_text[:12000],
            },
            "output_format": {"answers": {"Field Label": "truthful answer"}},
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.1,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)
            answers = self._safe_dict(data.get("answers"))
            return self._sanitize_form_answers(answers=answers, known_answers=known_answers)
        except Exception:
            return {}

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

    def _build_candidate_knowledge_json(
        self,
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
    ) -> dict[str, Any]:
        source_payload = {
            "profile": profile,
            "known_answers": known_answers,
            "cv_text": cv_text[:20000],
        }
        cache_key = self._context_hash(source_payload)
        if cache_key == self._candidate_knowledge_cache_key and self._candidate_knowledge_cache:
            return self._candidate_knowledge_cache

        fallback = self._fallback_candidate_knowledge_json(profile, known_answers, cv_text)
        if not self.enabled:
            self._candidate_knowledge_cache_key = cache_key
            self._candidate_knowledge_cache = fallback
            return fallback

        system_prompt = (
            "You synthesize candidate information into a compact factual JSON.\n"
            "Use only provided profile, known answers, and CV text.\n"
            "Never invent or infer unsupported facts.\n"
            "Preserve evidence snippets to justify skills/experience.\n"
            "Return JSON only."
        )
        user_payload = {
            "source": source_payload,
            "output_format": {
                "candidate_knowledge": {
                    "summary": "short factual summary",
                    "skills": ["strings"],
                    "tools": ["strings"],
                    "domains": ["strings"],
                    "experience_highlights": ["short bullet facts"],
                    "languages": ["strings"],
                    "work_preferences": {"key": "value"},
                    "contact": {"key": "value"},
                    "salary": {"key": "value"},
                    "evidence_snippets": ["verbatim short snippets from source"],
                }
            },
        }

        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
            )
            raw = completion.choices[0].message.content or "{}"
            data = self._safe_json(raw)
            candidate_knowledge = data.get("candidate_knowledge")
            if not isinstance(candidate_knowledge, dict) or not candidate_knowledge:
                candidate_knowledge = fallback
        except Exception:
            candidate_knowledge = fallback

        if "profile" not in candidate_knowledge:
            candidate_knowledge["profile"] = profile
        if "known_answers" not in candidate_knowledge:
            candidate_knowledge["known_answers"] = known_answers

        self._candidate_knowledge_cache_key = cache_key
        self._candidate_knowledge_cache = candidate_knowledge
        return candidate_knowledge

    @staticmethod
    def _fallback_candidate_knowledge_json(
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
    ) -> dict[str, Any]:
        lines = [line.strip() for line in cv_text.splitlines() if line.strip()]
        highlights: list[str] = []
        for line in lines:
            compact = " ".join(line.split())
            if len(compact) < 24:
                continue
            if compact in highlights:
                continue
            highlights.append(compact)
            if len(highlights) >= 40:
                break

        return {
            "summary": "Candidate knowledge synthesized directly from profile, known answers, and CV text.",
            "profile": profile,
            "known_answers": known_answers,
            "experience_highlights": highlights[:20],
            "evidence_snippets": highlights[:20],
        }

    @staticmethod
    def _context_hash(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _sanitize_form_answers(self, answers: dict[str, str], known_answers: dict[str, str]) -> dict[str, str]:
        if not answers:
            return {}

        known_blob = self._normalize_text(" ".join(known_answers.values()))
        sanitized: dict[str, str] = {}
        for label, answer in answers.items():
            clean_answer = str(answer).strip()
            clean_label = str(label).strip()
            if not clean_label or not clean_answer:
                continue

            if self._looks_negative_experience_statement(clean_answer):
                if not self._negative_is_supported(clean_answer, known_blob):
                    continue

            sanitized[clean_label] = clean_answer
        return sanitized

    @staticmethod
    def _heuristic_stuck_strategy(
        candidates: list[dict[str, str]],
        page_signals: dict[str, Any],
        validation_messages: list[str],
        recent_actions: list[str],
    ) -> tuple[str, int | None, str]:
        if bool(page_signals.get("requires_external_login")):
            return "wait_human", None, "External login detected."
        if bool(page_signals.get("has_hcaptcha")) or bool(page_signals.get("has_recaptcha")):
            return "wait_human", None, "Captcha detected."
        if validation_messages:
            return "fill_and_retry", None, "Validation issues present."

        recent_norm = [str(item).strip().lower() for item in recent_actions if str(item).strip()]
        repeated = len(recent_norm) >= 3 and len(set(recent_norm[-3:])) == 1
        if repeated:
            for candidate in candidates:
                label = str(candidate.get("label", "")).strip().lower()
                if not label:
                    continue
                if any(token in label for token in ("close", "cancel", "discard", "back", "login", "sign in", "privacy")):
                    continue
                try:
                    candidate_id = int(candidate.get("id", -1))
                except Exception:
                    candidate_id = -1
                if candidate_id >= 0:
                    return "click_candidate", candidate_id, "Try alternative action to break repetition."

        return "wait_and_retry", None, "No safe deterministic action from heuristic."

    @staticmethod
    def _negative_is_supported(answer: str, known_blob: str) -> bool:
        normalized_answer = LLMJobAgent._normalize_text(answer)
        if not known_blob:
            return False
        if normalized_answer and normalized_answer in known_blob:
            return True
        explicit_negative_markers = ("brak doswiadczenia", "no experience", "nie mam doswiadczenia")
        return any(marker in known_blob for marker in explicit_negative_markers)

    @staticmethod
    def _looks_negative_experience_statement(answer: str) -> bool:
        normalized_answer = LLMJobAgent._normalize_text(answer)
        negative_markers = (
            "nie posiadam doswiadczenia",
            "nie mam doswiadczenia",
            "brak doswiadczenia",
            "nie prowadzilem",
            "nie prowadzilam",
            "i do not have experience",
            "i dont have experience",
            "i don't have experience",
            "no experience",
            "i have not",
            "i havent",
            "i haven't",
        )
        experience_markers = (
            "doswiadczen",
            "experience",
            "agentic",
            "rag",
            "workshop",
            "training",
            "low code",
            "low-code",
            "mendix",
            "ai",
        )
        return any(marker in normalized_answer for marker in negative_markers) and any(
            marker in normalized_answer for marker in experience_markers
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        normalized = normalized.encode("ascii", "ignore").decode("ascii", "ignore")
        normalized = normalized.lower().strip()
        normalized = re.sub(r"[^a-z0-9 ]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    @staticmethod
    def _coerce_discovery_job_payload(job: Any) -> dict[str, str]:
        if isinstance(job, dict):
            title = str(job.get("title", "")).strip()
            company = str(job.get("company", "")).strip()
            location = str(job.get("location", "")).strip()
            snippet = str(job.get("snippet", "")).strip()
            url = str(job.get("url", "")).strip()
            job_id = str(job.get("job_id", "")).strip()
            source_query = str(job.get("source_query", "")).strip()
        else:
            title = str(getattr(job, "title", "")).strip()
            company = str(getattr(job, "company", "")).strip()
            location = str(getattr(job, "location", "")).strip()
            snippet = str(getattr(job, "snippet", "")).strip()
            url = str(getattr(job, "url", "")).strip()
            job_id = str(getattr(job, "job_id", "")).strip()
            source_query = str(getattr(job, "source_query", "")).strip()

        return {
            "job_id": job_id,
            "title": title,
            "company": company,
            "location": location,
            "snippet": snippet[:1200],
            "url": url,
            "source_query": source_query,
        }

    def _heuristic_discovery_score(
        self,
        job_payload: dict[str, str],
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
        preferences: dict[str, Any] | None = None,
    ) -> DiscoveryScore:
        prefs = preferences or {}
        normalized_text = self._normalize_text(
            " ".join(
                [
                    job_payload.get("title", ""),
                    job_payload.get("company", ""),
                    job_payload.get("location", ""),
                    job_payload.get("snippet", ""),
                    job_payload.get("source_query", ""),
                ]
            )
        )
        job_tokens = {token for token in normalized_text.split() if len(token) >= 3}
        candidate_tokens = self._extract_candidate_keywords(profile, known_answers, cv_text)

        overlap = candidate_tokens.intersection(job_tokens)
        overlap_ratio = len(overlap) / max(1, min(20, len(candidate_tokens)))
        skill_score = self._safe_int(20 + int(overlap_ratio * 180))

        title_norm = self._normalize_text(job_payload.get("title", ""))
        years = self._heuristic_years_of_experience(profile, known_answers)
        required_years = 2
        if any(token in title_norm for token in ("principal", "staff", "head", "director")):
            required_years = 8
        elif any(token in title_norm for token in ("senior", "lead")):
            required_years = 6
        elif any(token in title_norm for token in ("junior", "intern", "trainee")):
            required_years = 1
        if years >= required_years + 2:
            experience_score = 88
        elif years >= required_years:
            experience_score = 76
        elif years + 2 >= required_years:
            experience_score = 62
        else:
            experience_score = 42

        location_norm = self._normalize_text(job_payload.get("location", ""))
        remote_like = any(token in location_norm for token in ("remote", "hybrid", "eu", "emea"))
        poland_like = any(
            token in location_norm
            for token in ("poland", "polska", "warsaw", "warszawa", "krakow", "wroclaw", "gdansk", "poznan", "lodz")
        )
        outside_poland_tokens = (
            "germany",
            "france",
            "spain",
            "italy",
            "netherlands",
            "united kingdom",
            "ireland",
            "sweden",
            "norway",
            "denmark",
            "finland",
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
        has_outside_poland = any(token in location_norm for token in outside_poland_tokens)

        constraint_score = 74
        hard_reject = False
        reject_reason = ""

        prefer_poland = bool(prefs.get("prefer_poland", True))
        hard_reject_outside = bool(prefs.get("hard_reject_outside_poland", False))
        if prefer_poland and has_outside_poland and not poland_like and not remote_like:
            constraint_score = 30
            if hard_reject_outside:
                hard_reject = True
                reject_reason = "Location appears outside Poland without remote flexibility."

        include_tokens = [
            self._normalize_text(str(token))
            for token in prefs.get("keywords_include", [])
            if self._normalize_text(str(token))
        ]
        exclude_tokens = [
            self._normalize_text(str(token))
            for token in prefs.get("keywords_exclude", [])
            if self._normalize_text(str(token))
        ]
        if include_tokens:
            include_hits = sum(1 for token in include_tokens if token in normalized_text)
            if include_hits == 0:
                constraint_score = min(constraint_score, 45)
            else:
                constraint_score = self._safe_int(constraint_score + min(20, include_hits * 6))
        if exclude_tokens:
            excluded = [token for token in exclude_tokens if token in normalized_text]
            if excluded:
                constraint_score = min(constraint_score, 15)
                if bool(prefs.get("hard_reject_excluded_keywords", True)):
                    hard_reject = True
                    reject_reason = f"Contains excluded keywords: {', '.join(excluded[:3])}"

        snippet_norm = self._normalize_text(job_payload.get("snippet", ""))
        if "easy apply" in snippet_norm or "easy apply" in normalized_text:
            applyability_score = 90
        elif any(token in snippet_norm for token in ("apply on company website", "external", "workday", "greenhouse")):
            applyability_score = 48
        else:
            applyability_score = 65

        priority = self._compute_discovery_priority(
            skill=skill_score,
            experience=experience_score,
            constraint=constraint_score,
            applyability=applyability_score,
        )
        if hard_reject:
            priority = min(priority, 20)

        reasoning = (
            f"Heuristic scoring: overlap={len(overlap)} candidate_tokens={len(candidate_tokens)}, "
            f"years={years}, location='{job_payload.get('location', '')}'."
        )
        return DiscoveryScore(
            skill_match_score=skill_score,
            experience_match_score=experience_score,
            constraint_score=self._safe_int(constraint_score),
            applyability_score=applyability_score,
            priority_score=priority,
            hard_reject=hard_reject,
            reject_reason=reject_reason[:260],
            reasoning=reasoning[:420],
        )

    @staticmethod
    def _heuristic_years_of_experience(profile: dict[str, str], known_answers: dict[str, str]) -> int:
        candidate_values: list[str] = []
        for key in ("years_of_experience", "experience_years"):
            value = str(profile.get(key, "")).strip()
            if value:
                candidate_values.append(value)
        for key, value in known_answers.items():
            key_norm = LLMJobAgent._normalize_text(str(key))
            if "year" in key_norm and "experience" in key_norm:
                candidate_values.append(str(value))
        for raw in candidate_values:
            match = re.search(r"(\d{1,2})", raw)
            if match:
                try:
                    return max(0, min(40, int(match.group(1))))
                except ValueError:
                    continue
        return 3

    @staticmethod
    def _compute_discovery_priority(skill: int, experience: int, constraint: int, applyability: int) -> int:
        raw = 0.40 * float(skill) + 0.25 * float(experience) + 0.20 * float(constraint) + 0.15 * float(applyability)
        return max(0, min(100, int(round(raw))))

    @staticmethod
    def _extract_candidate_keywords(
        profile: dict[str, str],
        known_answers: dict[str, str],
        cv_text: str,
    ) -> set[str]:
        raw_text_parts: list[str] = []
        for key in ("current_title", "core_skills", "professional_summary", "city", "country"):
            value = str(profile.get(key, "")).strip()
            if value:
                raw_text_parts.append(value)
        for key, value in known_answers.items():
            key_norm = LLMJobAgent._normalize_text(str(key))
            if any(token in key_norm for token in ("skill", "stack", "technology", "experience", "title")):
                raw_text_parts.append(str(value))
        if cv_text:
            raw_text_parts.append(cv_text[:8000])

        normalized = LLMJobAgent._normalize_text(" ".join(raw_text_parts))
        stop_words = {
            "and",
            "the",
            "with",
            "for",
            "from",
            "this",
            "that",
            "are",
            "you",
            "your",
            "have",
            "has",
            "was",
            "were",
            "job",
            "role",
            "work",
            "using",
            "experience",
            "years",
            "year",
            "poland",
            "remote",
        }
        tokens = [token for token in normalized.split() if len(token) >= 3 and token not in stop_words]
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
            if len(unique_tokens) >= 120:
                break
        return set(unique_tokens)

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
            requires_work_outside_poland=LLMJobAgent._heuristic_requires_outside_poland(job),
            location_restriction_reasoning="Heuristic location check based on job location text.",
        )

    @staticmethod
    def _heuristic_requires_outside_poland(job: JobPosting) -> bool:
        location = (job.location or "").lower()
        if not location:
            return False

        keep_tokens = (
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
            "remote",
            "hybrid",
            "eu",
            "emea",
        )
        if any(token in location for token in keep_tokens):
            return False

        outside_tokens = (
            "germany",
            "berlin",
            "munich",
            "france",
            "paris",
            "spain",
            "madrid",
            "barcelona",
            "netherlands",
            "amsterdam",
            "united kingdom",
            "london",
            "ireland",
            "dublin",
            "sweden",
            "stockholm",
            "norway",
            "denmark",
            "finland",
            "portugal",
            "italy",
            "switzerland",
            "austria",
            "czech",
            "prague",
            "bratislava",
            "hungary",
            "budapest",
            "romania",
            "bucharest",
            "bulgaria",
            "sofia",
            "usa",
            "united states",
            "new york",
            "san francisco",
            "canada",
            "toronto",
            "vancouver",
            "australia",
        )
        return any(token in location for token in outside_tokens)
