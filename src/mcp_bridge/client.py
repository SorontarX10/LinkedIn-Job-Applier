from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class McpResponse:
    ok: bool
    result: dict[str, Any]
    error: str = ""


class McpStdioClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        timeout_sec: int = 8,
    ) -> None:
        self.command = command
        self.args = args
        self.env = env or {}
        self.timeout_sec = max(1, int(timeout_sec))
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._responses: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._stop_reader = threading.Event()
        self._lock = threading.Lock()
        self._next_id = 1

    def start(self) -> None:
        if self._proc is not None:
            return
        merged_env = None
        if self.env:
            merged_env = dict(os.environ)
            merged_env.update(self.env)

        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        self._stop_reader.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        self._initialize()

    def stop(self) -> None:
        self._stop_reader.set()
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> McpResponse:
        if self._proc is None:
            return McpResponse(ok=False, result={}, error="MCP process not started.")
        payload = {
            "name": tool_name,
            "arguments": arguments,
        }
        return self._request(method="tools/call", params=payload)

    def list_tools(self) -> McpResponse:
        return self._request(method="tools/list", params={})

    def _initialize(self) -> None:
        init_payload = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "linkedin-job-applier", "version": "1.0.0"},
        }
        _ = self._request(method="initialize", params=init_payload)
        self._notify(method="notifications/initialized", params={})

    def _notify(self, *, method: str, params: dict[str, Any]) -> None:
        request = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send_message(request)

    def _request(self, *, method: str, params: dict[str, Any]) -> McpResponse:
        if self._proc is None:
            return McpResponse(ok=False, result={}, error="MCP process not started.")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            try:
                self._send_message(request)
            except Exception as exc:
                return McpResponse(ok=False, result={}, error=f"Send failed: {exc}")

            deadline = threading.Event()
            # deadline flag used only to satisfy type checker for timeout loop readability.
            _ = deadline
            while True:
                try:
                    message = self._responses.get(timeout=self.timeout_sec)
                except queue.Empty:
                    return McpResponse(ok=False, result={}, error=f"Timeout waiting MCP response for {method}.")

                response_id = message.get("id")
                if response_id != request_id:
                    # Not our response; skip.
                    continue
                if "error" in message:
                    return McpResponse(ok=False, result={}, error=str(message.get("error")))
                result = message.get("result")
                if not isinstance(result, dict):
                    result = {"value": result}
                return McpResponse(ok=True, result=result)

    def _send_message(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("MCP process stdin unavailable.")
        encoded = json.dumps(message, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii")
        proc.stdin.write(header)
        proc.stdin.write(encoded)
        proc.stdin.flush()

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        while not self._stop_reader.is_set():
            try:
                message = self._read_message(stream)
            except Exception:
                break
            if message is None:
                break
            self._responses.put(message)

    @staticmethod
    def _read_message(stream: Any) -> dict[str, Any] | None:
        content_length = 0
        while True:
            line = stream.readline()
            if not line:
                return None
            line_text = line.decode("utf-8", errors="replace").strip()
            if not line_text:
                break
            lower = line_text.lower()
            if lower.startswith("content-length:"):
                try:
                    content_length = int(line_text.split(":", 1)[1].strip())
                except Exception:
                    content_length = 0
        if content_length <= 0:
            return None
        body = stream.read(content_length)
        if not body:
            return None
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}
