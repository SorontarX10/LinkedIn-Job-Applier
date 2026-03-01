from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.mcp_bridge.runtime_config import load_runtime_config


class McpRuntimeConfigTest(unittest.TestCase):
    def test_loads_runtime_config_with_sink_tool_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp_servers.runtime.json"
            payload = {
                "servers": {
                    "linear": {"enabled": True, "command": "python", "args": ["-m", "fake.linear"]},
                    "notion": {"enabled": True, "command": "python", "args": ["-m", "fake.notion"]},
                },
                "sinks": {
                    "linear": {
                        "enabled": True,
                        "server": "linear",
                        "tools": {
                            "create_issue": "save_issue",
                            "create_comment": "create_comment",
                            "update_issue": "save_issue",
                        },
                    },
                    "notion": {
                        "enabled": True,
                        "server": "notion",
                        "tools": {
                            "create_page": "notion-create-pages",
                            "update_page": "notion-update-page",
                        },
                    },
                },
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            cfg = load_runtime_config(path)
            self.assertIn("linear", cfg.servers)
            self.assertEqual(cfg.linear.server, "linear")
            self.assertEqual(cfg.linear.tools.create_issue, "save_issue")
            self.assertEqual(cfg.notion.tools.create_page, "notion-create-pages")


if __name__ == "__main__":
    unittest.main()

