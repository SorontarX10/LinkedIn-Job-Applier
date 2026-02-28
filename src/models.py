from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class JobPosting:
    job_id: str
    url: str
    title: str
    company: str
    location: str
    description: str
    apply_mode: str


@dataclass
class FitDecision:
    should_apply: bool
    fit_score: int
    reasoning: str
    requires_work_outside_poland: bool = False
    location_restriction_reasoning: str = ""
    missing_information: list[str] = field(default_factory=list)
    tailored_cv_notes: list[str] = field(default_factory=list)
    cover_letter: str = ""
    prefilled_answers: dict[str, str] = field(default_factory=dict)


@dataclass
class DiscoveryScore:
    skill_match_score: int
    experience_match_score: int
    constraint_score: int
    applyability_score: int
    priority_score: int
    hard_reject: bool = False
    reject_reason: str = ""
    reasoning: str = ""


@dataclass
class QueuedJob:
    job_id: str
    url: str
    source: str
    score: int = 0
    status: str = "queued"
    title: str = ""
    company: str = ""
    location: str = ""
    notes: str = ""
    last_status: str = ""
    attempt_count: int = 0
    retry_count: int = 0
    last_attempt_at_utc: str = ""
    created_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class ApplicationRecord:
    url: str
    title: str
    company: str
    status: str
    applied_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    notes: str = ""
