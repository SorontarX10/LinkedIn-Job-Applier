from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


COUNTERS = {
    "issue": 0,
    "comment": 0,
    "page": 0,
}


def write_message(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def read_message() -> dict[str, Any] | None:
    content_length = 0
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            break
        if text.lower().startswith("content-length:"):
            try:
                content_length = int(text.split(":", 1)[1].strip())
            except Exception:
                content_length = 0
    if content_length <= 0:
        return None
    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def tool_result(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    sleep_sec = float(os.getenv("FAKE_MCP_SLEEP_SEC", "0"))
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    fail_tools = {
        item.strip()
        for item in os.getenv("FAKE_MCP_FAIL_TOOLS", "").split(",")
        if item.strip()
    }
    if name in fail_tools:
        raise RuntimeError(f"Tool configured to fail: {name}")

    if name == "save_issue":
        if arguments.get("id"):
            return {"id": str(arguments["id"]), "updated": True}
        COUNTERS["issue"] += 1
        return {"id": f"ISSUE-{COUNTERS['issue']}", "created": True}
    if name == "create_comment":
        COUNTERS["comment"] += 1
        return {"id": f"COMMENT-{COUNTERS['comment']}", "created": True}
    if name == "notion-create-pages":
        COUNTERS["page"] += 1
        return {"page_id": f"PAGE-{COUNTERS['page']}"}
    if name == "notion-update-page":
        return {"updated": True}
    if name == "generate_diagram":
        return {"url": "https://figma.example/diagram/1"}
    return {"ok": True, "tool": name}


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})

        if method == "initialize":
            if request_id is not None:
                write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
                        },
                    }
                )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            if request_id is not None:
                write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "tools": [
                                {"name": "save_issue"},
                                {"name": "create_comment"},
                                {"name": "notion-create-pages"},
                                {"name": "notion-update-page"},
                                {"name": "generate_diagram"},
                            ]
                        },
                    }
                )
            continue

        if method == "tools/call":
            name = str(params.get("name", "")).strip()
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = tool_result(name=name, arguments=arguments)
                if request_id is not None:
                    write_message({"jsonrpc": "2.0", "id": request_id, "result": result})
            except Exception as exc:
                if request_id is not None:
                    write_message(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32000, "message": str(exc)},
                        }
                    )
            continue

        if request_id is not None:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()

