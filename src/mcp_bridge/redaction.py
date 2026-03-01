from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?\d[\d\-\s()]{7,}\d")
OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")
GENERIC_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{30,}\b")

SENSITIVE_KEY_TOKENS = (
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
    "h-captcha-response",
    "captcha",
    "cv_text",
    "raw_html",
    "html_excerpt",
)


def redact_text(text: str, max_len: int = 1200) -> str:
    value = str(text)
    value = EMAIL_RE.sub("[redacted_email]", value)
    value = PHONE_RE.sub("[redacted_phone]", value)
    value = OPENAI_KEY_RE.sub("[redacted_openai_key]", value)
    value = GENERIC_TOKEN_RE.sub(_mask_long_token, value)
    if len(value) > max_len:
        value = f"{value[: max_len - 3]}..."
    return value


def redact_payload(payload: Any) -> Any:
    if payload is None:
        return None
    if isinstance(payload, (bool, int, float)):
        return payload
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, str):
        return redact_text(payload)
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload[:80]]
    if isinstance(payload, tuple):
        return [redact_payload(item) for item in list(payload)[:80]]
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in list(payload.items())[:80]:
            normalized_key = str(key)
            if _looks_like_large_html(value):
                out[normalized_key] = _hashed_stub(str(value), prefix="html")
                continue
            if _is_sensitive_key(normalized_key):
                out[normalized_key] = "[redacted_sensitive_value]"
                continue
            out[normalized_key] = redact_payload(value)
        return out
    return redact_text(str(payload))


def normalize_error_signature(*parts: str) -> str:
    base = " | ".join(str(item).strip().lower() for item in parts if str(item).strip())
    normalized = re.sub(r"\s+", " ", base).strip()
    normalized = re.sub(r"[^a-z0-9|:_./ -]+", "", normalized)
    return normalized[:300]


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _is_sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    return any(token in lowered for token in SENSITIVE_KEY_TOKENS)


def _looks_like_large_html(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    sample = value.strip().lower()
    if len(sample) < 2000:
        return False
    return "<html" in sample or "<body" in sample or "<div" in sample


def _hashed_stub(text: str, *, prefix: str) -> str:
    digest = stable_hash(text)
    return f"[redacted_{prefix}_sha16:{digest}]"


def _mask_long_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.startswith("http://") or token.startswith("https://"):
        return token
    if len(token) < 48:
        return token
    return f"[redacted_token_sha16:{stable_hash(token)}]"
