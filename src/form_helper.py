from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

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
        self._manual_file_prompts_seen: set[str] = set()

    def add_prefilled_answers(self, answers: dict[str, str]) -> None:
        for key, value in answers.items():
            key_str = str(key).strip()
            value_str = str(value).strip()
            if not key_str or not value_str:
                continue
            self.prefilled_answers[key_str] = value_str
            self.prefilled_lookup[_normalize(key_str)] = value_str

    def collect_visible_fields_snapshot(self, page: Page) -> list[dict[str, Any]]:
        if self._is_page_closed(page):
            return []

        snapshot: list[dict[str, Any]] = []
        open_list_options = self._extract_open_listbox_options(page)

        text_fields = page.locator(
            "input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([disabled]), "
            "textarea:not([disabled])"
        )
        for idx in range(min(text_fields.count(), 120)):
            field = text_fields.nth(idx)
            if not self._is_interactable(field):
                continue
            label = self._safe_label(field)
            if not label:
                continue
            context = self._field_context(field)
            field_type = (field.get_attribute("type") or self._tag_name(field) or "text").lower()
            value = ""
            placeholder = ""
            try:
                value = (field.input_value() or "").strip()
            except Exception:
                value = ""
            try:
                placeholder = (field.get_attribute("placeholder") or "").strip()
            except Exception:
                placeholder = ""
            snapshot.append(
                {
                    "label": label[:180],
                    "kind": "text",
                    "type": field_type[:40],
                    "required": self._is_required(field),
                    "context": context[:500],
                    "placeholder": placeholder[:160],
                    "current_value": value[:160],
                }
            )

        selects = page.locator("select:not([disabled])")
        for idx in range(min(selects.count(), 80)):
            select = selects.nth(idx)
            if not self._is_interactable(select):
                continue
            label = self._safe_label(select)
            if not label:
                continue
            context = self._field_context(select)
            options = self._extract_select_options(select)
            current_value = ""
            try:
                raw_value = (select.input_value() or "").strip()
            except Exception:
                raw_value = ""
            try:
                selected_text = str(
                    select.evaluate(
                        """(el) => {
                            const idx = el.selectedIndex;
                            if (idx < 0) return '';
                            const option = el.options[idx];
                            return option ? ((option.textContent || '').trim()) : '';
                        }"""
                    )
                ).strip()
            except Exception:
                selected_text = ""
            if raw_value and not self._is_placeholder_choice(raw_value):
                current_value = raw_value
            elif selected_text and not self._is_placeholder_choice(selected_text):
                current_value = selected_text
            snapshot.append(
                {
                    "label": label[:180],
                    "kind": "select",
                    "required": self._is_required(select),
                    "context": context[:500],
                    "current_value": current_value[:160],
                    "options": options[:25],
                }
            )

        combos = page.locator(
            "[role='combobox']:not([aria-disabled='true']), "
            "input[aria-autocomplete='list']:not([disabled]), "
            "div[role='button'][aria-haspopup='listbox']:not([aria-disabled='true']), "
            "button[aria-haspopup='listbox']:not([disabled])"
        )
        for idx in range(min(combos.count(), 80)):
            combo = combos.nth(idx)
            if not self._is_interactable(combo):
                continue
            label = self._safe_label(combo)
            if not label:
                continue
            context = self._field_context(combo)
            current_value = ""
            try:
                if self._tag_name(combo) == "input":
                    current_value = (combo.input_value() or "").strip()
                else:
                    current_value = (combo.inner_text() or "").strip()
            except Exception:
                current_value = ""
            snapshot.append(
                {
                    "label": label[:180],
                    "kind": "combobox",
                    "required": self._is_required(combo),
                    "context": context[:500],
                    "current_value": current_value[:160],
                    "options": open_list_options[:25],
                }
            )

        radios = page.locator("input[type='radio']")
        radio_groups: dict[str, dict[str, Any]] = {}
        for idx in range(min(radios.count(), 120)):
            radio = radios.nth(idx)
            if not self._is_interactable(radio):
                continue
            name = (radio.get_attribute("name") or f"_radio_{idx}").strip()
            group = radio_groups.setdefault(
                name,
                {
                    "label": self._safe_label(radio),
                    "kind": "radio",
                    "required": self._is_required(radio),
                    "context": self._field_context(radio)[:500],
                    "options": [],
                    "current_value": "",
                },
            )
            option_label = self._safe_label(radio)
            if option_label:
                group["options"].append(option_label[:120])
            try:
                if radio.is_checked() and option_label:
                    group["current_value"] = option_label[:120]
            except Exception:
                pass
        snapshot.extend(radio_groups.values())

        return snapshot

    def fill_visible_fields(self, page: Page) -> None:
        if self._is_page_closed(page):
            return

        steps = (
            self._fill_file_inputs,
            self._fill_text_fields,
            self._fill_combobox_fields,
            self._fill_select_fields,
            self._fill_radio_fields,
            self._fill_checkboxes,
        )
        for step in steps:
            if self._is_page_closed(page):
                return
            try:
                step(page)
            except Exception:
                if self._is_page_closed(page):
                    return
                continue

    def _fill_file_inputs(self, page: Page) -> None:
        file_inputs = page.locator("input[type='file']")
        for index in range(file_inputs.count()):
            locator = file_inputs.nth(index)
            label = self._safe_label(locator)
            normalized_label = _normalize(label)
            context = self._field_context(locator)

            if self._file_input_has_file(locator):
                continue

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
                    continue

                self._handoff_manual_file_upload(locator=locator, label=label, context=context)
            except Exception:
                continue

    def _fill_text_fields(self, page: Page) -> None:
        fields = page.locator(
            "input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([disabled]):not([aria-autocomplete='list']):not([list]), "
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
            context = self._field_context(field)

            required = self._is_required(field)
            answer = self._resolve_answer(label, required=required, context=context)

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
                selected_text = ""
                try:
                    selected_text = str(
                        select.evaluate(
                            """(el) => {
                                const idx = el.selectedIndex;
                                if (idx < 0) return '';
                                const option = el.options[idx];
                                return option ? ((option.textContent || '').trim()) : '';
                            }"""
                        )
                    ).strip()
                except Exception:
                    selected_text = ""
                if not self._is_placeholder_choice(existing) and not self._is_placeholder_choice(selected_text):
                    continue

            label = self._safe_label(select)
            context = self._field_context(select)
            required = self._is_required(select)
            answer = self._resolve_answer(label, required=required, context=context)
            if not answer:
                options = self._extract_select_options(select)
                answer = self._select_fallback_answer(label, context, options)
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

    def _fill_combobox_fields(self, page: Page) -> None:
        combos = page.locator(
            "[role='combobox']:not([aria-disabled='true']), "
            "input[aria-autocomplete='list']:not([disabled]), "
            "input[list]:not([disabled]), "
            "div[role='button'][aria-haspopup='listbox']:not([aria-disabled='true']), "
            "button[aria-haspopup='listbox']:not([disabled])"
        )

        seen_labels: set[str] = set()
        for index in range(min(combos.count(), 90)):
            combo = combos.nth(index)
            if not self._is_interactable(combo):
                continue

            label = self._safe_label(combo)
            normalized_label = _normalize(label)
            if not normalized_label or normalized_label in seen_labels:
                continue
            seen_labels.add(normalized_label)
            context = self._field_context(combo)

            if self._combobox_has_value(combo):
                continue

            required = self._is_required(combo)
            answer = self._resolve_answer(label, required=required, context=context)
            if not answer:
                open_options = self._extract_open_listbox_options(page)
                answer = self._select_fallback_answer(label, context, open_options)
            if not answer:
                continue

            try:
                combo.click()
                page.wait_for_timeout(180)
            except Exception:
                continue

            tag = self._tag_name(combo)
            if tag == "input":
                try:
                    combo.fill(answer)
                    page.wait_for_timeout(260)
                except Exception:
                    pass

            if self._select_open_option(page, answer):
                if tag == "input":
                    try:
                        combo.press("Tab")
                    except Exception:
                        pass
                continue

            if tag == "input":
                try:
                    combo.press("ArrowDown")
                    page.wait_for_timeout(120)
                    combo.press("Enter")
                    page.wait_for_timeout(180)
                    combo.press("Tab")
                except Exception:
                    continue

    def _combobox_has_value(self, combo: Locator) -> bool:
        tag = self._tag_name(combo)
        if tag == "input":
            try:
                value = (combo.input_value() or "").strip()
            except Exception:
                return False
            if not value:
                return False
            if self._is_autocomplete_input(combo) and not self._autocomplete_selection_committed(combo):
                return False
            return True
        try:
            text = (combo.inner_text() or "").strip()
        except Exception:
            return False
        normalized = _normalize(text)
        return bool(normalized) and not self._is_placeholder_choice(text)

    def _select_open_option(self, page: Page, answer: str) -> bool:
        options = page.locator(
            "[role='listbox'] [role='option'], "
            "[role='option'], "
            "li[role='option'], "
            ".artdeco-typeahead__result, "
            ".select2-results__option, "
            ".dropdown-results > div, "
            ".dropdown-results li, "
            "[id*='-option-']"
        )
        normalized_answer = _normalize(answer)
        candidates: list[tuple[Locator, str]] = []
        deadline = time.time() + 2.2
        while time.time() < deadline:
            try:
                if options.count() > 0:
                    break
            except Exception:
                return False
            try:
                page.wait_for_timeout(140)
            except Exception:
                return False

        for idx in range(min(options.count(), 140)):
            option = options.nth(idx)
            if not self._is_interactable(option):
                continue
            try:
                text = (option.inner_text() or "").strip()
            except Exception:
                text = ""
            if text:
                candidates.append((option, text))

        if not candidates:
            return False

        for option, text in candidates:
            if _normalize(text) == normalized_answer:
                try:
                    option.click()
                    return True
                except Exception:
                    continue

        for option, text in candidates:
            normalized_text = _normalize(text)
            if normalized_answer in normalized_text or normalized_text in normalized_answer:
                try:
                    option.click()
                    return True
                except Exception:
                    continue

        best_option: Locator | None = None
        best_score = -1
        for option, text in candidates:
            score = max(
                fuzz.partial_ratio(normalized_answer, _normalize(text)),
                fuzz.token_set_ratio(normalized_answer, _normalize(text)),
            )
            if score > best_score:
                best_score = score
                best_option = option

        if best_option is None or best_score < 70:
            return False
        try:
            best_option.click()
            return True
        except Exception:
            return False

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
            context = self._field_context(first_radio)
            required = self._is_required(first_radio)
            answer = self._resolve_answer(question_label, required=required, context=context)
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

            # Opt out from "follow/obserwuj company" style checkboxes by default.
            if any(token in label for token in ("follow company", "follow", "obserwuj", "obserwowanie")) and checked:
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

    def _resolve_answer(self, label: str, required: bool, context: str = "") -> str:
        combined_label = label if not context else f"{label} | {context[:220]}"
        normalized_base_label = _normalize(label)
        normalized_combined_label = _normalize(combined_label)
        if normalized_base_label in self.prefilled_lookup:
            return self.prefilled_lookup[normalized_base_label]
        if normalized_combined_label in self.prefilled_lookup:
            return self.prefilled_lookup[normalized_combined_label]

        salary_based = self._salary_answer_for_text_question(label, context)
        if salary_based:
            return salary_based
        notice_based = self._notice_period_answer_for_text_question(label, context)
        if notice_based:
            return notice_based

        if self.prefilled_lookup:
            best = process.extractOne(
                normalized_base_label,
                list(self.prefilled_lookup.keys()),
                scorer=fuzz.token_set_ratio,
            )
            if best and best[1] >= 78:
                return self.prefilled_lookup[best[0]]
            best = process.extractOne(
                normalized_combined_label,
                list(self.prefilled_lookup.keys()),
                scorer=fuzz.token_set_ratio,
            )
            if best and best[1] >= 82:
                return self.prefilled_lookup[best[0]]

        known = self.knowledge.get_known_answer(combined_label)
        if known:
            return known

        known = self.knowledge.get_known_answer(label)
        if known:
            return known

        experience_based = self._experience_answer_for_text_question(label, context)
        if experience_based:
            return experience_based

        return self.knowledge.get_or_ask_answer(combined_label, required=required)

    def _append_disclosure(self, text: str) -> str:
        clean = text.strip()
        if not self.ai_disclosure_text:
            return clean
        if _normalize(self.ai_disclosure_text) in _normalize(clean):
            return clean
        if not clean:
            return self.ai_disclosure_text
        return f"{clean}\n\n{self.ai_disclosure_text}"

    def _experience_answer_for_text_question(self, label: str, context: str) -> str:
        return ""

    def _has_candidate_evidence(self, clues: tuple[str, ...]) -> bool:
        _ = clues
        return False

    def _choose_yes_no_option(self, question: str, options: list[str]) -> str:
        _ = question
        _ = options
        return ""

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
            if self._is_placeholder_choice(text):
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

    def _extract_select_options(self, select: Locator) -> list[str]:
        try:
            options = select.evaluate(
                """(el) => Array.from(el.options).map((option) =>
                    ((option.textContent || '').trim())
                )"""
            )
        except Exception:
            return []
        if not isinstance(options, list):
            return []
        parsed = []
        for item in options:
            text = str(item).strip()
            if text and not self._is_placeholder_choice(text):
                parsed.append(text[:120])
        return parsed

    def _extract_open_listbox_options(self, page: Page) -> list[str]:
        options = page.locator("[role='listbox'] [role='option'], [role='option'], li[role='option']")
        parsed: list[str] = []
        for idx in range(min(options.count(), 80)):
            option = options.nth(idx)
            if not self._is_interactable(option):
                continue
            try:
                text = (option.inner_text() or "").strip()
            except Exception:
                text = ""
            if text and not self._is_placeholder_choice(text):
                parsed.append(text[:120])
        return parsed

    def _select_fallback_answer(self, label: str, context: str, options: list[str]) -> str:
        if not options:
            return ""
        question = _normalize(f"{label} {context}")
        expected_salary = self._expected_salary_text()

        yes_no_choice = self._choose_yes_no_option(question, options)
        if yes_no_choice:
            return yes_no_choice

        if any(token in question for token in ("how did you find", "find about", "source", "job post")):
            for preferred in ("LinkedIn", "LinkedIn job post", "Social media post on LinkedIn"):
                normalized_pref = _normalize(preferred)
                for option in options:
                    normalized_option = _normalize(option)
                    if normalized_pref == normalized_option or normalized_pref in normalized_option:
                        return option

        if any(token in question for token in ("english", "language", "proficiency")):
            for preferred in ("proficient", "advanced", "fluent", "c2", "c1", "intermediate", "basic"):
                normalized_pref = _normalize(preferred)
                for option in options:
                    normalized_option = _normalize(option)
                    if normalized_pref == normalized_option or normalized_pref in normalized_option:
                        return option

        if any(token in question for token in ("currency", "walut")):
            preferred_currency = "PLN"
            if "eur" in _normalize(expected_salary):
                preferred_currency = "EUR"
            elif "usd" in _normalize(expected_salary):
                preferred_currency = "USD"
            elif "gbp" in _normalize(expected_salary):
                preferred_currency = "GBP"
            elif "chf" in _normalize(expected_salary):
                preferred_currency = "CHF"
            for option in options:
                normalized_option = _normalize(option)
                if _normalize(preferred_currency) == normalized_option or _normalize(preferred_currency) in normalized_option:
                    return option

        if any(token in question for token in ("salary period", "period", "wynagrodzenie")):
            preferred_period = "Annual" if any(token in _normalize(expected_salary) for token in ("annual", "year", "rocznie")) else "Month"
            for option in options:
                normalized_option = _normalize(option)
                if _normalize(preferred_period) == normalized_option or _normalize(preferred_period) in normalized_option:
                    return option

        if any(token in question for token in ("gross or net", "brutto", "netto")):
            preferred = "net" if "net" in _normalize(expected_salary) or "netto" in _normalize(expected_salary) else "gross"
            for option in options:
                normalized_option = _normalize(option)
                if preferred == normalized_option or preferred in normalized_option:
                    return option

        if any(token in question for token in ("agreement", "contract", "umow", "employment type", "rodzaj umowy")):
            for preferred in (
                "B2B",
                "umowa o pracÄ™",
                "umowa o prace",
                "permanent",
                "employment contract",
                "any option is okay",
            ):
                normalized_pref = _normalize(preferred)
                for option in options:
                    normalized_option = _normalize(option)
                    if normalized_pref == normalized_option or normalized_pref in normalized_option:
                        return option

        country_preference = (
            self.knowledge.profile.get("country", "").strip()
            or self.knowledge.get_known_answer("country")
            or ""
        ).strip()
        if not country_preference and any(
            token in question for token in ("country", "countries", "residence", "based", "work from", "right to work")
        ):
            country_preference = "Poland"
        if country_preference:
            normalized_pref = _normalize(country_preference)
            for option in options:
                if _normalize(option) == normalized_pref:
                    return option
            for option in options:
                if normalized_pref in _normalize(option):
                    return option

        if any(
            token in question
            for token in ("country", "countries", "residence", "work from", "based in", "right to work", "authorization")
        ):
            for preferred in ("Poland", "Polska"):
                normalized_pref = _normalize(preferred)
                for option in options:
                    if normalized_pref == _normalize(option) or normalized_pref in _normalize(option):
                        return option
        return ""

    def _salary_answer_for_text_question(self, label: str, context: str) -> str:
        question = _normalize(f"{label} {context}")
        if "salary" not in question and "wynagrod" not in question and "compensation" not in question:
            return ""

        expected_salary = self._expected_salary_text()
        minimum, maximum = self._extract_salary_range(expected_salary)
        if minimum <= 0 and maximum <= 0:
            return ""
        if maximum <= 0:
            maximum = minimum
        midpoint = (minimum + maximum) / 2 if minimum and maximum else maximum or minimum

        target_currency = "PLN"
        for currency in ("EUR", "USD", "GBP", "CHF", "PLN"):
            if _normalize(currency) in question:
                target_currency = currency
                break

        converted = self._convert_pln_amount(midpoint, target_currency)
        converted_int = int(round(converted))
        if "only numbers" in question or "number" in question or "digits" in question:
            return str(max(0, converted_int))

        yearly_markers = ("annual", "year", "rocznie", "yearly", "per year")
        suffix = "year"
        if any(marker in question for marker in ("month", "monthly", "mies")):
            suffix = "month"
            converted_int = int(round(converted / 12))
        elif any(marker in question for marker in yearly_markers):
            suffix = "year"
        return f"{max(0, converted_int)} {target_currency}/{suffix}"

    def _notice_period_answer_for_text_question(self, label: str, context: str) -> str:
        question = _normalize(f"{label} {context}")
        if not any(
            token in question
            for token in (
                "notice period",
                "okres wypowiedzenia",
                "kiedy mozesz zaczac",
                "when can you start",
                "when can you join",
                "start date",
                "od kiedy mozesz dolaczyc",
            )
        ):
            return ""

        profile_notice = self.knowledge.profile.get("notice_period", "").strip()
        if profile_notice:
            return profile_notice
        return (self.knowledge.get_known_answer("notice period") or "").strip()

    def _expected_salary_text(self) -> str:
        profile_salary = self.knowledge.profile.get("expected_salary", "").strip()
        if profile_salary:
            return profile_salary
        known_salary = self.knowledge.get_known_answer("expected salary")
        return known_salary.strip()

    @staticmethod
    def _extract_salary_range(text: str) -> tuple[float, float]:
        if not text.strip():
            return 0.0, 0.0

        normalized_text = text.lower()
        matches = re.findall(r"\d+(?:[.,]\d+)?", normalized_text)
        if not matches:
            return 0.0, 0.0

        values: list[float] = []
        for token in matches[:2]:
            cleaned = token.replace(",", ".")
            try:
                values.append(float(cleaned))
            except ValueError:
                continue
        if not values:
            return 0.0, 0.0

        multiplier = 1.0
        if any(marker in normalized_text for marker in ("tys", "thousand", " k", "k ")):
            multiplier = 1000.0
        if max(values) < 1000 and multiplier == 1000.0:
            values = [value * multiplier for value in values]
        minimum = values[0]
        maximum = values[1] if len(values) > 1 else values[0]
        return minimum, maximum

    @staticmethod
    def _convert_pln_amount(amount_pln: float, target_currency: str) -> float:
        rates_from_pln = {
            "PLN": 1.0,
            "EUR": 4.35,
            "USD": 4.00,
            "GBP": 5.10,
            "CHF": 4.60,
        }
        divisor = rates_from_pln.get(target_currency.upper(), 1.0)
        if divisor <= 0:
            return amount_pln
        return amount_pln / divisor

    @staticmethod
    def _is_placeholder_choice(value: str) -> bool:
        normalized = _normalize(value)
        if not normalized:
            return True

        simple_placeholders = {
            "select",
            "choose",
            "wybierz",
            "select option",
            "choose option",
            "select an option",
            "choose an option",
            "please select",
            "please choose",
            "none",
            "n a",
            "na",
        }
        if normalized in simple_placeholders:
            return True
        if normalized.startswith("select ") and "option" in normalized:
            return True
        if normalized.startswith("choose ") and "option" in normalized:
            return True
        return False

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
    def _tag_name(field: Locator) -> str:
        try:
            tag = field.evaluate("(el) => el.tagName.toLowerCase()")
            return str(tag).strip().lower()
        except Exception:
            return ""

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
    def _is_page_closed(page: Page) -> bool:
        try:
            return page.is_closed()
        except Exception:
            return True

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
    def _file_input_has_file(locator: Locator) -> bool:
        try:
            selected = locator.evaluate("(el) => Boolean(el.files && el.files.length > 0)")
            return bool(selected)
        except Exception:
            return False

    def _handoff_manual_file_upload(self, locator: Locator, label: str, context: str) -> None:
        prompt_key = _normalize(f"{label}|{context}")
        if prompt_key and prompt_key in self._manual_file_prompts_seen:
            return

        field_name = label.strip() or context.strip() or "Unnamed file field"
        print(
            f"[Manual upload] File field detected: {field_name}. "
            "Upload the file in browser; bot resumes automatically."
        )
        deadline = time.time() + 240
        while time.time() < deadline:
            if self._file_input_has_file(locator):
                break
            try:
                locator.page.wait_for_timeout(700)
            except Exception:
                break
        if prompt_key:
            self._manual_file_prompts_seen.add(prompt_key)
        if self._file_input_has_file(locator):
            return

    @staticmethod
    def _is_autocomplete_input(combo: Locator) -> bool:
        try:
            aria_auto = (combo.get_attribute("aria-autocomplete") or "").strip().lower()
            if aria_auto == "list":
                return True
            list_attr = (combo.get_attribute("list") or "").strip()
            if list_attr:
                return True
        except Exception:
            return False
        return False

    @staticmethod
    def _autocomplete_selection_committed(combo: Locator) -> bool:
        try:
            committed = combo.evaluate(
                """(el) => {
                    const root =
                        el.closest('.application-field, .application-question, .application-additional, .form-group, [role="group"]') ||
                        el.parentElement ||
                        document;
                    const hiddenInputs = root.querySelectorAll('input[type="hidden"]');
                    let hasRelevantHidden = false;
                    for (const hidden of hiddenInputs) {
                        const key = `${hidden.id || ''} ${hidden.name || ''}`.toLowerCase();
                        if (key.includes('location') || key.includes('selected') || key.includes('option') || key.includes('value')) {
                            hasRelevantHidden = true;
                            if ((hidden.value || '').trim()) return true;
                        }
                    }
                    if (!hasRelevantHidden) return true;
                    return false;
                }"""
            )
            return bool(committed)
        except Exception:
            return True

    @staticmethod
    def _safe_label(locator: Locator) -> str:
        try:
            label = locator.evaluate(
                """(el) => {
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();

                    const byAriaLabel = el.getAttribute('aria-label');
                    if (byAriaLabel) return clean(byAriaLabel);

                    const labelledBy = el.getAttribute('aria-labelledby');
                    if (labelledBy) {
                        const labels = [];
                        for (const id of labelledBy.split(' ')) {
                            const node = document.getElementById(id.trim());
                            if (node && node.textContent) labels.push(clean(node.textContent));
                        }
                        if (labels.length > 0) return clean(labels.join(' '));
                    }

                    if (el.id) {
                        const directLabel = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (directLabel && directLabel.textContent) return clean(directLabel.textContent);
                    }

                    const leverQuestion = el.closest('.application-question, .application-additional');
                    if (leverQuestion) {
                        const richLabel = leverQuestion.querySelector('.application-label .text, .application-label');
                        if (richLabel && richLabel.textContent) {
                            return clean(richLabel.textContent.replace(/âś±|\\*/g, ' '));
                        }
                    }

                    const parentLabel = el.closest('label');
                    if (parentLabel) {
                        const richLabel = parentLabel.querySelector('.application-label .text, .application-label');
                        if (richLabel && richLabel.textContent) {
                            return clean(richLabel.textContent.replace(/âś±|\\*/g, ' '));
                        }

                        // Avoid returning entire label contents (inputs/options/errors).
                        const clone = parentLabel.cloneNode(true);
                        clone.querySelectorAll('input,select,textarea,button,script,style,svg').forEach((node) => node.remove());
                        const compact = clean(clone.textContent || '');
                        if (compact) return compact;
                    }

                    const fieldset = el.closest('fieldset');
                    if (fieldset) {
                        const legend = fieldset.querySelector('legend');
                        if (legend && legend.textContent) return clean(legend.textContent);
                    }

                    return clean(
                        el.getAttribute('placeholder') ||
                        el.getAttribute('name') ||
                        el.getAttribute('id') ||
                        ''
                    );
                }"""
            )
            return str(label or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _field_context(locator: Locator) -> str:
        try:
            context = locator.evaluate(
                """(el) => {
                    const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                    const chunks = [];

                    const descBy = el.getAttribute('aria-describedby');
                    if (descBy) {
                        for (const id of descBy.split(' ')) {
                            const node = document.getElementById(id.trim());
                            if (node && node.textContent) chunks.push(clean(node.textContent));
                        }
                    }

                    const question = el.closest('.application-question, .application-additional, fieldset, .jobs-easy-apply-form-section__grouping, .jobs-easy-apply-form-element, .fb-form-element, .artdeco-text-input');
                    if (question) {
                        const labelNode = question.querySelector('.application-label .text, .application-label');
                        if (labelNode && labelNode.textContent) chunks.push(clean(labelNode.textContent));

                        const descNodes = question.querySelectorAll('.description, .form-text, small');
                        for (let i = 0; i < Math.min(descNodes.length, 3); i += 1) {
                            const text = clean(descNodes[i].textContent || '');
                            if (text) chunks.push(text);
                        }
                    }

                    const prev = el.previousElementSibling;
                    if (prev && prev.textContent) chunks.push(clean(prev.textContent));

                    const next = el.nextElementSibling;
                    if (next && next.textContent) chunks.push(clean(next.textContent));

                    return chunks
                        .filter(Boolean)
                        .join(' | ')
                        .slice(0, 600)
                        .trim();
                }"""
            )
            return str(context or "").strip()
        except Exception:
            return ""

