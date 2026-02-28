from __future__ import annotations

import unittest

from src.agentic_tools import AgenticToolExecutor


class _FakePage:
    def is_closed(self) -> bool:
        return False


class _FakeLocator:
    def __init__(self) -> None:
        self.clicked = False

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self.clicked = True


class AgenticToolSafetyTest(unittest.TestCase):
    def test_blocked_action_label_is_rejected(self) -> None:
        executor = AgenticToolExecutor(blocked_action_tokens=("discard", "logout"))
        page = _FakePage()
        locator = _FakeLocator()
        executor._action_cache[0] = locator
        executor._action_label_cache[0] = "Discard application"

        result = executor.execute("click_action", page=page, candidate_id=0)
        self.assertFalse(result.ok)
        self.assertIn("Blocked action label", result.error)
        self.assertFalse(locator.clicked)

    def test_allowed_action_label_is_clicked(self) -> None:
        executor = AgenticToolExecutor(blocked_action_tokens=("discard", "logout"))
        page = _FakePage()
        locator = _FakeLocator()
        executor._action_cache[0] = locator
        executor._action_label_cache[0] = "Continue"

        result = executor.execute("click_action", page=page, candidate_id=0)
        self.assertTrue(result.ok)
        self.assertTrue(locator.clicked)


if __name__ == "__main__":
    unittest.main()
