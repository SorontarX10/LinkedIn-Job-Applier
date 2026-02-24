from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
    missing_information: list[str] = field(default_factory=list)
    tailored_cv_notes: list[str] = field(default_factory=list)
    cover_letter: str = ""
    prefilled_answers: dict[str, str] = field(default_factory=dict)


@dataclass
class ApplicationRecord:
    url: str
    title: str
    company: str
    status: str
    applied_at_utc: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds"))
    notes: str = ""
