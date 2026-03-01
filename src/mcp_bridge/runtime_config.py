from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class LinearSinkToolConfig:
    create_issue: str = "save_issue"
    create_comment: str = "create_comment"
    update_issue: str = "save_issue"


@dataclass
class NotionSinkToolConfig:
    create_page: str = "notion-create-pages"
    update_page: str = "notion-update-page"


@dataclass
class FigmaSinkToolConfig:
    generate_diagram: str = "generate_diagram"


@dataclass
class LinearSinkRuntime:
    server: str = "linear"
    enabled: bool = True
    tools: LinearSinkToolConfig = field(default_factory=LinearSinkToolConfig)


@dataclass
class NotionSinkRuntime:
    server: str = "notion"
    enabled: bool = True
    tools: NotionSinkToolConfig = field(default_factory=NotionSinkToolConfig)


@dataclass
class FigmaSinkRuntime:
    server: str = "figma"
    enabled: bool = False
    tools: FigmaSinkToolConfig = field(default_factory=FigmaSinkToolConfig)


@dataclass
class McpRuntimeConfig:
    servers: dict[str, McpServerConfig] = field(default_factory=dict)
    linear: LinearSinkRuntime = field(default_factory=LinearSinkRuntime)
    notion: NotionSinkRuntime = field(default_factory=NotionSinkRuntime)
    figma: FigmaSinkRuntime = field(default_factory=FigmaSinkRuntime)

    @staticmethod
    def defaults() -> "McpRuntimeConfig":
        return McpRuntimeConfig(
            servers={},
            linear=LinearSinkRuntime(),
            notion=NotionSinkRuntime(),
            figma=FigmaSinkRuntime(),
        )


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return []


def _parse_server(name: str, raw: dict[str, Any]) -> McpServerConfig | None:
    command = str(raw.get("command", "")).strip()
    if not command:
        return None
    args = [str(item) for item in _as_list(raw.get("args")) if str(item).strip()]
    env = {str(k): str(v) for k, v in _as_dict(raw.get("env")).items() if str(k).strip()}
    enabled = _as_bool(raw.get("enabled"), True)
    return McpServerConfig(name=name, command=command, args=args, env=env, enabled=enabled)


def load_runtime_config(path: Path) -> McpRuntimeConfig:
    if not path.exists():
        return McpRuntimeConfig.defaults()

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return McpRuntimeConfig.defaults()

    if not isinstance(raw, dict):
        return McpRuntimeConfig.defaults()

    cfg = McpRuntimeConfig.defaults()

    servers_raw = _as_dict(raw.get("servers"))
    servers: dict[str, McpServerConfig] = {}
    for key, value in servers_raw.items():
        parsed = _parse_server(str(key).strip(), _as_dict(value))
        if parsed is not None:
            servers[parsed.name] = parsed
    cfg.servers = servers

    linear_raw = _as_dict(_as_dict(raw.get("sinks")).get("linear")) or _as_dict(raw.get("linear"))
    notion_raw = _as_dict(_as_dict(raw.get("sinks")).get("notion")) or _as_dict(raw.get("notion"))
    figma_raw = _as_dict(_as_dict(raw.get("sinks")).get("figma")) or _as_dict(raw.get("figma"))

    if linear_raw:
        cfg.linear.enabled = _as_bool(linear_raw.get("enabled"), cfg.linear.enabled)
        cfg.linear.server = str(linear_raw.get("server", cfg.linear.server)).strip() or cfg.linear.server
        linear_tools = _as_dict(linear_raw.get("tools"))
        if linear_tools:
            cfg.linear.tools = LinearSinkToolConfig(
                create_issue=str(linear_tools.get("create_issue", cfg.linear.tools.create_issue)).strip()
                or cfg.linear.tools.create_issue,
                create_comment=str(linear_tools.get("create_comment", cfg.linear.tools.create_comment)).strip()
                or cfg.linear.tools.create_comment,
                update_issue=str(linear_tools.get("update_issue", cfg.linear.tools.update_issue)).strip()
                or cfg.linear.tools.update_issue,
            )

    if notion_raw:
        cfg.notion.enabled = _as_bool(notion_raw.get("enabled"), cfg.notion.enabled)
        cfg.notion.server = str(notion_raw.get("server", cfg.notion.server)).strip() or cfg.notion.server
        notion_tools = _as_dict(notion_raw.get("tools"))
        if notion_tools:
            cfg.notion.tools = NotionSinkToolConfig(
                create_page=str(notion_tools.get("create_page", cfg.notion.tools.create_page)).strip()
                or cfg.notion.tools.create_page,
                update_page=str(notion_tools.get("update_page", cfg.notion.tools.update_page)).strip()
                or cfg.notion.tools.update_page,
            )

    if figma_raw:
        cfg.figma.enabled = _as_bool(figma_raw.get("enabled"), cfg.figma.enabled)
        cfg.figma.server = str(figma_raw.get("server", cfg.figma.server)).strip() or cfg.figma.server
        figma_tools = _as_dict(figma_raw.get("tools"))
        if figma_tools:
            cfg.figma.tools = FigmaSinkToolConfig(
                generate_diagram=str(figma_tools.get("generate_diagram", cfg.figma.tools.generate_diagram)).strip()
                or cfg.figma.tools.generate_diagram,
            )

    return cfg

