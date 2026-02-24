from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Locator, Page
from rapidfuzz import fuzz, process

from src.knowledge_store import KnowledgeStore


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return text


class FormHelper:
    COMMENT_FIELD_TOKENS = (
        "comment",
        "comments",
        "additional information",
        "additional comments",
        "message",
        "notes",
        "anything else",
        "other information",
        "uwagi",
        "komentarz",
        "dodatkowe informacje",
    )

    def __init__(
        self,
        knowledge: KnowledgeStore,
        cv_path: Path,
        cover_letter_path: Path | None = None,
        prefilled_answers: dict[str, str] | None = None,
        ai_disclosure_text: str = "",
    ):
        self.knowledge = knowledge
        self.cv_path = cv_path
        self.cover_letter_path = cover_letter_path
        self.prefilled_answers = prefilled_answers or {}
        self.prefilled_lookup = {_normalize(key): value for key, value in self.prefilled_answers.items()}
        self.ai_disclosure_text = ai_disclosure_text.strip()

    def fill_visible_fields(self, page: Page) -> None:
        self._fill_file_inputs(page)
        self._fill_text_fields(page)
        self._fill_select_fields(page)
        self._fill_radio_fields(page)
        self._fill_checkboxes(page)

    def _fill_file_inputs(self, page: Page) -> None:
        file_inputs = page.locator("input[type='file']")
        for index in range(file_inputs.count()):
            locator = file_inputs.nth(index)
            label = self._safe_label(locator)
            normalized_label = _normalize(label)

            try:
                if any(token in normalized_label for token in ("cover letter", "motivation", "list motywacyjny")):
                    if self.cover_letter_path and self.cover_letter_path.exists():
                        locator.set_input_files(str(self.cover_letter_path))
                    continue

                if any(token in normalized_label for token in ("resume", "cv", "zyciorys")):
                    locator.set_input_files(str(self.cv_path))
                    continue

                if file_inputs.count() == 1:
                    locator.set_input_files(str(self.cv_path))
            except Exception:
                continue

    def _fill_text_fields(self, page: Page) -> None:
        fields = page.locator(
            "input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([disabled]), "
            "textarea:not([disabled])"
        )

        for index in range(fields.count()):
            field = fields.nth(index)
            if not self._is_interactable(field):
                continue

            try:
                current = (field.input_value() or "").strip()
            except Exception:
                current = ""
            if current:
                continue

            label = self._safe_label(field)
            if not label:
                continue
            normalized_label = _normalize(label)

            required = self._is_required(field)
            answer = self._resolve_answer(label, required=required)

            is_comment_field = self._is_comment_field(field, normalized_label)
            if not answer and is_comment_field and self.ai_disclosure_text:
                answer = self.ai_disclosure_text
            if not answer:
                continue

            field_type = (field.get_attribute("type") or "text").lower()
            if field_type == "number":
                answer = self._extract_first_number(answer)
                if not answer:
                    continue

            if "cover letter" in normalized_label and self.cover_letter_path and self.cover_letter_path.exists():
                answer = self.cover_letter_path.read_text(encoding="utf-8")

            if is_comment_field and self.ai_disclosure_text:
                answer = self._append_disclosure(answer)

            answer = self._fit_to_max_length(field, answer)
            if not answer:
                continue

            try:
                field.fill(answer)
            except Exception:
                continue

    def _fill_select_fields(self, page: Page) -> None:
        selects = page.locator("select:not([disabled])")
        for index in range(selects.count()):
            select = selects.nth(index)
            if not self._is_interactable(select):
                continue

            try:
                existing = select.input_value().strip()
            except Exception:
                existing = ""
            if existing:
                continue

            label = self._safe_label(select)
            required = self._is_required(select)
            answer = self._resolve_answer(label, required=required)
            if not answer:
                continue

            option = self._best_option(select, answer)
            if not option:
                continue

            try:
                if option.get("value"):
                    select.select_option(value=str(option["value"]))
                elif option.get("text"):
                    select.select_option(label=str(option["text"]))
            except Exception:
                continue

    def _fill_radio_fields(self, page: Page) -> None:
        radios = page.locator("input[type='radio']")
        groups: dict[str, list[int]] = {}

        for index in range(radios.count()):
            radio = radios.nth(index)
            if not self._is_interactable(radio):
                continue
            name = (radio.get_attribute("name") or f"_radio_{index}").strip()
            groups.setdefault(name, []).append(index)

        for indices in groups.values():
            first_radio = radios.nth(indices[0])
            question_label = self._safe_label(first_radio)
            required = self._is_required(first_radio)
            answer = self._resolve_answer(question_label, required=required)
            if not answer:
                continue

            matched = False
            for idx in indices:
                option_radio = radios.nth(idx)
                option_label = self._safe_label(option_radio)
                if self._radio_option_matches(answer, option_label):
                    try:
                        option_radio.check()
                        matched = True
                        break
                    except Exception:
                        continue

            if not matched:
                lowered = _normalize(answer)
                if lowered in {"yes", "tak"}:
                    for idx in indices:
                        option_radio = radios.nth(idx)
                        option_label = _normalize(self._safe_label(option_radio))
                        if "yes" in option_label or "tak" in option_label:
                            try:
                                option_radio.check()
                            except Exception:
                                pass
                            break
                elif lowered in {"no", "nie"}:
                    for idx in indices:
                        option_radio = radios.nth(idx)
                        option_label = _normalize(self._safe_label(option_radio))
                        if "no" in option_label or "nie" in option_label:
                            try:
                                option_radio.check()
                            except Exception:
                                pass
                            break

    def _fill_checkboxes(self, page: Page) -> None:
        checkboxes = page.locator("input[type='checkbox']")
        for index in range(checkboxes.count()):
            checkbox = checkboxes.nth(index)
            if not self._is_interactable(checkbox):
                continue

            label = _normalize(self._safe_label(checkbox))
            required = self._is_required(checkbox)
            try:
                checked = checkbox.is_checked()
            except Exception:
                checked = False

            # LinkedIn often pre-checks "follow company"; opt out by default.
            if "follow company" in label and checked:
                try:
                    checkbox.uncheck()
                except Exception:
                    pass
                continue

            if required and not checked:
                try:
                    checkbox.check()
                except Exception:
                    continue

    def _resolve_answer(self, label: str, required: bool) -> str:
        normalized_label = _normalize(label)
        if normalized_label in self.prefilled_lookup:
            return self.prefilled_lookup[normalized_label]

        if self.prefilled_lookup:
            best = process.extractOne(normalized_label, list(self.prefilled_lookup.keys()), scorer=fuzz.token_set_ratio)
            if best and best[1] >= 82:
                return self.prefilled_lookup[best[0]]

        known = self.knowledge.get_known_answer(label)
        if known:
            return known

        return self.knowledge.get_or_ask_answer(label, required=required)

    def _append_disclosure(self, text: str) -> str:
        clean = text.strip()
        if not self.ai_disclosure_text:
            return clean
        if _normalize(self.ai_disclosure_text) in _normalize(clean):
            return clean
        if not clean:
            return self.ai_disclosure_text
        return f"{clean}\n\n{self.ai_disclosure_text}"

    def _best_option(self, select: Locator, answer: str) -> dict[str, str] | None:
        try:
            options = select.evaluate(
                """(el) => Array.from(el.options).map((option) => ({
                    value: option.value || '',
                    text: (option.textContent || '').trim()
                }))"""
            )
        except Exception:
            return None

        if not isinstance(options, list):
            return None

        normalized_answer = _normalize(answer)
        candidates = []
        for option in options:
            text = str(option.get("text", "")).strip()
            value = str(option.get("value", "")).strip()
            if not text and not value:
                continue
            if _normalize(text) in {"select", "choose", "wybierz"}:
                continue
            candidates.append({"text": text, "value": value})

        for option in candidates:
            if _normalize(option["text"]) == normalized_answer or _normalize(option["value"]) == normalized_answer:
                return option

        for option in candidates:
            if normalized_answer in _normalize(option["text"]) or normalized_answer in _normalize(option["value"]):
                return option

        best_option: dict[str, str] | None = None
        best_score = -1
        for option in candidates:
            score = max(
                fuzz.partial_ratio(normalized_answer, _normalize(option["text"])),
                fuzz.token_set_ratio(normalized_answer, _normalize(option["text"])),
                fuzz.partial_ratio(normalized_answer, _normalize(option["value"])),
            )
            if score > best_score:
                best_score = score
                best_option = option

        if best_score >= 68:
            return best_option
        return None

    @staticmethod
    def _extract_first_number(text: str) -> str:
        match = re.search(r"-?\d+(\.\d+)?", text)
        if match:
            return match.group(0)
        return ""

    def _is_comment_field(self, field: Locator, normalized_label: str) -> bool:
        if any(token in normalized_label for token in self.COMMENT_FIELD_TOKENS):
            return True
        if "cover letter" in normalized_label:
            return True
        return self._is_textarea(field) and (
            "motivation" in normalized_label or "why" in normalized_label or "introduce yourself" in normalized_label
        )

    @staticmethod
    def _is_textarea(field: Locator) -> bool:
        try:
            tag = field.evaluate("(el) => el.tagName.toLowerCase()")
            return str(tag).strip().lower() == "textarea"
        except Exception:
            return False

    @staticmethod
    def _fit_to_max_length(field: Locator, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        try:
            max_length_raw = field.get_attribute("maxlength") or ""
            max_length = int(max_length_raw) if max_length_raw.strip() else 0
            if max_length > 0 and len(text) > max_length:
                return text[:max_length].rstrip()
        except Exception:
            return text
        return text

    @staticmethod
    def _radio_option_matches(answer: str, option_label: str) -> bool:
        normalized_answer = _normalize(answer)
        normalized_option = _normalize(option_label)
        if not normalized_option:
            return False
        if normalized_answer in normalized_option or normalized_option in normalized_answer:
            return True
        return fuzz.token_set_ratio(normalized_answer, normalized_option) >= 72

    @staticmethod
    def _is_interactable(locator: Locator) -> bool:
        try:
            return locator.is_visible() and locator.is_enabled()
        except Exception:
            return False

    @staticmethod
    def _is_required(locator: Locator) -> bool:
        try:
            if locator.get_attribute("required") is not None:
                return True
            aria_required = locator.get_attribute("aria-required") or ""
            return aria_required.lower() == "true"
        except Exception:
            return False

    @staticmethod
    def _safe_label(locator: Locator) -> str:
        try:
            label = locator.evaluate(
                """(el) => {
                    const byAriaLabel = el.getAttribute('aria-label');
                    if (byAriaLabel) return byAriaLabel.trim();

                    const labelledBy = el.getAttribute('aria-labelledby');
                    if (labelledBy) {
                        const node = document.getElementById(labelledBy);
                        if (node && node.textContent) return node.textContent.trim();
                    }

                    if (el.id) {
                        const directLabel = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (directLabel && directLabel.textContent) return directLabel.textContent.trim();
                    }

                    const parentLabel = el.closest('label');
                    if (parentLabel && parentLabel.textContent) return parentLabel.textContent.trim();

                    const fieldset = el.closest('fieldset');
                    if (fieldset) {
                        const legend = fieldset.querySelector('legend');
                        if (legend && legend.textContent) return legend.textContent.trim();
                    }

                    return (
                        el.getAttribute('placeholder') ||
                        el.getAttribute('name') ||
                        el.getAttribute('id') ||
                        ''
                    ).trim();
                }"""
            )
            return str(label or "").strip()
        except Exception:
            return ""
