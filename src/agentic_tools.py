from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page


def _normalize(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9 ]+", "", lowered)
    return lowered


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class _FieldBinding:
    locator: Locator
    kind: str
    label: str
    options: list[str] = field(default_factory=list)


class AgenticToolExecutor:
    """Safe Playwright tool layer for agentic fallback.

    The executor keeps ephemeral caches for currently visible action candidates
    and fields. Tool calls are intentionally constrained and stateful:
    - call `list_actions` before `click_action`
    - call `list_form_fields` before field mutation tools
    """

    DEFAULT_BLOCKED_ACTION_TOKENS = (
        "discard",
        "close application",
        "withdraw",
        "delete",
        "remove",
        "logout",
        "log out",
        "sign out",
        "cancel application",
    )

    def __init__(
        self,
        max_steps_per_session: int = 40,
        max_session_seconds: int = 150,
        blocked_action_tokens: tuple[str, ...] | None = None,
    ) -> None:
        self.max_steps_per_session = max(1, int(max_steps_per_session))
        self.max_session_seconds = max(10, int(max_session_seconds))
        self.blocked_action_tokens = tuple(
            _normalize(token)
            for token in (blocked_action_tokens or self.DEFAULT_BLOCKED_ACTION_TOKENS)
            if str(token).strip()
        )
        self._session_started = time.time()
        self._steps_used = 0
        self._action_cache: dict[int, Locator] = {}
        self._action_label_cache: dict[int, str] = {}
        self._field_cache: dict[int, _FieldBinding] = {}

    def reset_session(self) -> None:
        self._session_started = time.time()
        self._steps_used = 0
        self._action_cache.clear()
        self._action_label_cache.clear()
        self._field_cache.clear()

    def _consume_step(self, tool_name: str) -> ToolResult | None:
        if self._steps_used >= self.max_steps_per_session:
            return ToolResult(ok=False, tool=tool_name, error="Step limit exceeded in agentic tool session.")
        if (time.time() - self._session_started) > self.max_session_seconds:
            return ToolResult(ok=False, tool=tool_name, error="Time limit exceeded in agentic tool session.")
        self._steps_used += 1
        return None

    def execute(
        self,
        tool_name: str,
        *,
        page: Page,
        root: Page | Locator | None = None,
        allow_plain_anchors: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        if self._is_page_closed(page):
            return ToolResult(ok=False, tool=tool_name, error="Page is closed.")

        guard = self._consume_step(tool_name)
        if guard is not None:
            return guard

        dispatch: dict[str, Any] = {
            "get_dom_snapshot": self.get_dom_snapshot,
            "list_actions": self.list_actions,
            "click_action": self.click_action,
            "list_form_fields": self.list_form_fields,
            "type_into_field": self.type_into_field,
            "select_option": self.select_option,
            "set_checkbox": self.set_checkbox,
            "set_file_input": self.set_file_input,
            "wait": self.wait,
            "scroll": self.scroll,
            "read_validation_messages": self.read_validation_messages,
            "detect_login_or_captcha": self.detect_login_or_captcha,
            "take_screenshot": self.take_screenshot,
        }
        handler = dispatch.get(tool_name)
        if handler is None:
            return ToolResult(ok=False, tool=tool_name, error=f"Unknown tool: {tool_name}")

        try:
            if tool_name == "list_actions":
                return handler(root=root or page, allow_plain_anchors=allow_plain_anchors, **kwargs)
            return handler(page=page, **kwargs)
        except Exception as exc:
            error_text = " ".join(str(exc).split())
            if len(error_text) > 220:
                error_text = f"{error_text[:217]}..."
            return ToolResult(ok=False, tool=tool_name, error=error_text)

    def get_dom_snapshot(self, page: Page, max_chars: int = 32000) -> ToolResult:
        raw_html = page.content() or ""
        compact = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw_html)
        compact = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", compact)
        compact = re.sub(r"\s+", " ", compact).strip()
        return ToolResult(
            ok=True,
            tool="get_dom_snapshot",
            data={
                "url": page.url,
                "raw_html_len": len(raw_html),
                "html_excerpt": compact[:max_chars],
            },
        )

    def list_actions(
        self,
        root: Page | Locator,
        allow_plain_anchors: bool = False,
        limit: int = 40,
    ) -> ToolResult:
        selector = "button, a[role='button'], input[type='submit'], input[type='button']"
        if allow_plain_anchors:
            selector += ", a"

        nodes = root.locator(selector)
        max_scan = min(nodes.count(), 300)

        actions: list[dict[str, str]] = []
        self._action_cache.clear()
        self._action_label_cache.clear()
        for idx in range(max_scan):
            node = nodes.nth(idx)
            if not self._is_interactable(node):
                continue
            label = self._safe_label(node)
            if not label:
                continue
            action_id = len(actions)
            role = (node.get_attribute("role") or "").strip()
            href = (node.get_attribute("href") or "").strip()
            node_type = (node.get_attribute("type") or "").strip()
            actions.append(
                {
                    "id": str(action_id),
                    "label": label[:180],
                    "role": role[:40],
                    "type": node_type[:30],
                    "href": href[:240],
                }
            )
            self._action_cache[action_id] = node
            self._action_label_cache[action_id] = label
            if len(actions) >= max(1, int(limit)):
                break

        return ToolResult(ok=True, tool="list_actions", data={"actions": actions})

    def click_action(self, page: Page, candidate_id: int) -> ToolResult:
        del page
        action = self._action_cache.get(int(candidate_id))
        if action is None:
            return ToolResult(ok=False, tool="click_action", error="Unknown candidate_id. Call list_actions first.")

        label = self._action_label_cache.get(int(candidate_id), "")
        normalized_label = _normalize(label)
        if normalized_label and any(token in normalized_label for token in self.blocked_action_tokens):
            return ToolResult(ok=False, tool="click_action", error=f"Blocked action label: {label}")

        if not self._is_interactable(action):
            return ToolResult(ok=False, tool="click_action", error="Candidate is no longer interactable.")

        action.click()
        return ToolResult(ok=True, tool="click_action", data={"candidate_id": int(candidate_id), "label": label})

    def list_form_fields(self, page: Page, limit: int = 80) -> ToolResult:
        fields: list[dict[str, Any]] = []
        self._field_cache.clear()

        # Text-like fields
        text_like = page.locator(
            "input:not([type='hidden']):not([type='file']):not([type='checkbox']):not([type='radio']):not([disabled]), "
            "textarea:not([disabled])"
        )
        for idx in range(min(text_like.count(), 240)):
            locator = text_like.nth(idx)
            if not self._is_interactable(locator):
                continue
            label = self._safe_label(locator)
            if not label:
                continue
            kind = "combobox" if self._is_combobox(locator) else "text"
            current_value = self._safe_input_value(locator)
            field_id = len(fields)
            binding = _FieldBinding(locator=locator, kind=kind, label=label)
            self._field_cache[field_id] = binding
            fields.append(
                {
                    "id": field_id,
                    "kind": kind,
                    "label": label[:180],
                    "required": self._is_required(locator),
                    "current_value": current_value[:180],
                }
            )
            if len(fields) >= max(1, int(limit)):
                return ToolResult(ok=True, tool="list_form_fields", data={"fields": fields})

        # Select fields
        selects = page.locator("select:not([disabled])")
        for idx in range(min(selects.count(), 120)):
            locator = selects.nth(idx)
            if not self._is_interactable(locator):
                continue
            label = self._safe_label(locator)
            if not label:
                continue
            options = self._extract_select_options(locator)
            current_value = self._safe_input_value(locator)
            field_id = len(fields)
            self._field_cache[field_id] = _FieldBinding(locator=locator, kind="select", label=label, options=options)
            fields.append(
                {
                    "id": field_id,
                    "kind": "select",
                    "label": label[:180],
                    "required": self._is_required(locator),
                    "current_value": current_value[:180],
                    "options": options[:25],
                }
            )
            if len(fields) >= max(1, int(limit)):
                return ToolResult(ok=True, tool="list_form_fields", data={"fields": fields})

        # Checkboxes
        checkboxes = page.locator("input[type='checkbox']:not([disabled])")
        for idx in range(min(checkboxes.count(), 120)):
            locator = checkboxes.nth(idx)
            if not self._is_interactable(locator):
                continue
            label = self._safe_label(locator)
            if not label:
                continue
            checked = False
            try:
                checked = locator.is_checked()
            except Exception:
                checked = False
            field_id = len(fields)
            self._field_cache[field_id] = _FieldBinding(locator=locator, kind="checkbox", label=label)
            fields.append(
                {
                    "id": field_id,
                    "kind": "checkbox",
                    "label": label[:180],
                    "required": self._is_required(locator),
                    "current_value": "true" if checked else "false",
                }
            )
            if len(fields) >= max(1, int(limit)):
                return ToolResult(ok=True, tool="list_form_fields", data={"fields": fields})

        # File inputs
        files = page.locator("input[type='file']:not([disabled])")
        for idx in range(min(files.count(), 40)):
            locator = files.nth(idx)
            if not self._is_interactable(locator):
                continue
            label = self._safe_label(locator)
            if not label:
                label = f"file_input_{idx}"
            field_id = len(fields)
            self._field_cache[field_id] = _FieldBinding(locator=locator, kind="file", label=label)
            fields.append(
                {
                    "id": field_id,
                    "kind": "file",
                    "label": label[:180],
                    "required": self._is_required(locator),
                    "current_value": "",
                }
            )
            if len(fields) >= max(1, int(limit)):
                break

        return ToolResult(ok=True, tool="list_form_fields", data={"fields": fields})

    def type_into_field(self, page: Page, field_id: int, value: str, clear_first: bool = True) -> ToolResult:
        del page
        binding = self._field_cache.get(int(field_id))
        if binding is None:
            return ToolResult(ok=False, tool="type_into_field", error="Unknown field_id. Call list_form_fields first.")
        if binding.kind not in {"text", "combobox"}:
            return ToolResult(ok=False, tool="type_into_field", error=f"Field kind does not support typing: {binding.kind}")
        if not self._is_interactable(binding.locator):
            return ToolResult(ok=False, tool="type_into_field", error="Field is no longer interactable.")
        text = str(value).strip()
        if not text:
            return ToolResult(ok=False, tool="type_into_field", error="Empty value.")
        if clear_first:
            binding.locator.fill(text)
        else:
            binding.locator.type(text)
        return ToolResult(ok=True, tool="type_into_field", data={"field_id": int(field_id), "label": binding.label})

    def select_option(self, page: Page, field_id: int, option_value: str) -> ToolResult:
        del page
        binding = self._field_cache.get(int(field_id))
        if binding is None:
            return ToolResult(ok=False, tool="select_option", error="Unknown field_id. Call list_form_fields first.")
        if binding.kind != "select":
            return ToolResult(ok=False, tool="select_option", error=f"Field kind does not support select: {binding.kind}")
        if not self._is_interactable(binding.locator):
            return ToolResult(ok=False, tool="select_option", error="Field is no longer interactable.")

        value = str(option_value).strip()
        if not value:
            return ToolResult(ok=False, tool="select_option", error="Empty option value.")

        selected = False
        try:
            binding.locator.select_option(label=value)
            selected = True
        except Exception:
            selected = False
        if not selected:
            try:
                binding.locator.select_option(value=value)
                selected = True
            except Exception:
                selected = False
        if not selected:
            best = self._best_option_match(value, binding.options)
            if best:
                try:
                    binding.locator.select_option(label=best)
                    selected = True
                except Exception:
                    selected = False
        if not selected:
            return ToolResult(ok=False, tool="select_option", error=f"Could not select option: {value}")

        return ToolResult(ok=True, tool="select_option", data={"field_id": int(field_id), "label": binding.label, "value": value})

    def set_checkbox(self, page: Page, field_id: int, checked: bool) -> ToolResult:
        del page
        binding = self._field_cache.get(int(field_id))
        if binding is None:
            return ToolResult(ok=False, tool="set_checkbox", error="Unknown field_id. Call list_form_fields first.")
        if binding.kind != "checkbox":
            return ToolResult(ok=False, tool="set_checkbox", error=f"Field kind is not checkbox: {binding.kind}")
        if not self._is_interactable(binding.locator):
            return ToolResult(ok=False, tool="set_checkbox", error="Field is no longer interactable.")

        if bool(checked):
            binding.locator.check()
        else:
            binding.locator.uncheck()
        return ToolResult(ok=True, tool="set_checkbox", data={"field_id": int(field_id), "label": binding.label, "checked": bool(checked)})

    def set_file_input(self, page: Page, field_id: int, file_path: str) -> ToolResult:
        del page
        binding = self._field_cache.get(int(field_id))
        if binding is None:
            return ToolResult(ok=False, tool="set_file_input", error="Unknown field_id. Call list_form_fields first.")
        if binding.kind != "file":
            return ToolResult(ok=False, tool="set_file_input", error=f"Field kind is not file: {binding.kind}")
        if not self._is_interactable(binding.locator):
            return ToolResult(ok=False, tool="set_file_input", error="Field is no longer interactable.")

        path = Path(str(file_path).strip())
        if not path.exists() or not path.is_file():
            return ToolResult(ok=False, tool="set_file_input", error=f"File path does not exist: {path}")
        binding.locator.set_input_files(str(path))
        return ToolResult(ok=True, tool="set_file_input", data={"field_id": int(field_id), "label": binding.label, "file_path": str(path)})

    def wait(self, page: Page, ms: int = 800) -> ToolResult:
        delay = max(100, min(int(ms), 15000))
        page.wait_for_timeout(delay)
        return ToolResult(ok=True, tool="wait", data={"ms": delay})

    def scroll(self, page: Page, px: int = 600) -> ToolResult:
        amount = max(-5000, min(int(px), 8000))
        page.evaluate("(delta) => window.scrollBy(0, delta)", amount)
        page.wait_for_timeout(180)
        return ToolResult(ok=True, tool="scroll", data={"px": amount})

    def read_validation_messages(self, page: Page, limit: int = 12) -> ToolResult:
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
                max_scan = min(locator.count(), 50)
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
                if len(messages) >= max(1, int(limit)):
                    return ToolResult(ok=True, tool="read_validation_messages", data={"messages": messages})
        return ToolResult(ok=True, tool="read_validation_messages", data={"messages": messages})

    def detect_login_or_captcha(self, page: Page) -> ToolResult:
        url = page.url.lower()
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

        login_url_hit = any(token in url for token in ("/login", "/signin", "/sign-in", "/auth/", "/checkpoint", "/join"))
        has_password = False
        has_identity = False
        try:
            has_password = page.locator("input[type='password'], input[name*='password']").count() > 0
        except Exception:
            has_password = False
        try:
            has_identity = page.locator("input[type='email'], input[name*='email'], input[name*='user']").count() > 0
        except Exception:
            has_identity = False

        requires_login = bool(login_url_hit or (has_password and has_identity))
        return ToolResult(
            ok=True,
            tool="detect_login_or_captcha",
            data={
                "url": page.url,
                "requires_login": requires_login,
                "has_hcaptcha": has_hcaptcha,
                "has_recaptcha": has_recaptcha,
            },
        )

    def take_screenshot(
        self,
        page: Page,
        output_dir: str = "output/agentic_traces",
        prefix: str = "agentic",
        full_page: bool = True,
    ) -> ToolResult:
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "agentic"
        out_path = target_dir / f"{safe_prefix}_{stamp}.png"
        page.screenshot(path=str(out_path), full_page=bool(full_page))
        return ToolResult(ok=True, tool="take_screenshot", data={"path": str(out_path)})

    @staticmethod
    def _safe_label(node: Locator) -> str:
        try:
            text = (node.inner_text() or "").strip()
        except Exception:
            text = ""
        if text:
            return " ".join(text.split())

        for attr in ("aria-label", "placeholder", "title", "name", "value"):
            try:
                value = (node.get_attribute(attr) or "").strip()
            except Exception:
                value = ""
            if value:
                return " ".join(value.split())

        try:
            from_id = (node.get_attribute("id") or "").strip()
        except Exception:
            from_id = ""
        if from_id:
            return from_id
        return ""

    @staticmethod
    def _safe_input_value(node: Locator) -> str:
        try:
            value = (node.input_value() or "").strip()
            if value:
                return value
        except Exception:
            pass
        try:
            value = (node.inner_text() or "").strip()
            return value
        except Exception:
            return ""

    @staticmethod
    def _is_interactable(node: Locator) -> bool:
        try:
            return node.is_visible() and node.is_enabled()
        except Exception:
            return False

    @staticmethod
    def _tag_name(node: Locator) -> str:
        try:
            return str(node.evaluate("(el) => (el.tagName || '').toLowerCase()")).strip()
        except Exception:
            return ""

    def _is_combobox(self, node: Locator) -> bool:
        try:
            role = (node.get_attribute("role") or "").strip().lower()
        except Exception:
            role = ""
        if role == "combobox":
            return True
        try:
            aria = (node.get_attribute("aria-autocomplete") or "").strip().lower()
        except Exception:
            aria = ""
        return aria == "list"

    @staticmethod
    def _is_required(node: Locator) -> bool:
        try:
            if node.get_attribute("required") is not None:
                return True
        except Exception:
            pass
        try:
            aria_required = (node.get_attribute("aria-required") or "").strip().lower()
        except Exception:
            aria_required = ""
        if aria_required == "true":
            return True
        label = AgenticToolExecutor._safe_label(node).lower()
        return "*" in label or "required" in label

    @staticmethod
    def _extract_select_options(select: Locator, limit: int = 60) -> list[str]:
        try:
            values = select.evaluate(
                """(el, maxItems) => {
                    const opts = Array.from(el.options || []);
                    const out = [];
                    for (const option of opts) {
                        const text = (option.textContent || '').trim();
                        if (!text) continue;
                        out.push(text);
                        if (out.length >= maxItems) break;
                    }
                    return out;
                }""",
                int(limit),
            )
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        cleaned: list[str] = []
        for value in values:
            text = " ".join(str(value).split()).strip()
            if not text:
                continue
            if text not in cleaned:
                cleaned.append(text[:140])
        return cleaned

    @staticmethod
    def _best_option_match(target: str, options: list[str]) -> str:
        target_norm = _normalize(target)
        if not target_norm:
            return ""
        best_option = ""
        best_score = 0.0
        for option in options:
            option_norm = _normalize(option)
            if not option_norm:
                continue
            if option_norm == target_norm:
                return option
            if target_norm in option_norm or option_norm in target_norm:
                return option
            score = AgenticToolExecutor._ratio(target_norm, option_norm)
            if score > best_score:
                best_score = score
                best_option = option
        if best_score >= 0.72:
            return best_option
        return ""

    @staticmethod
    def _ratio(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        common = 0
        for token in set(a.split()):
            if token in b:
                common += 1
        denom = max(len(set(a.split())), len(set(b.split())), 1)
        return common / denom

    @staticmethod
    def _is_page_closed(page: Page) -> bool:
        try:
            return page.is_closed()
        except Exception:
            return True
