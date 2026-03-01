# MCP Bridge Template (Reusable)

This folder contains a minimal reusable template for wiring MCP event publishing into other Python automation projects.

## Minimal steps

1. Copy `publisher_adapter.py` and adapt event hook points.
2. Copy `.env` keys from this project (`MCP_*` section).
3. Provide runtime server config in `data/mcp_servers.runtime.json`.
4. Call:
   - `publisher.start()` at app startup
   - `publisher.publish_event(...)` on key lifecycle events
   - `publisher.drain_spool()` at startup/shutdown
   - `publisher.stop()` during shutdown

## Required concepts

- fail-open semantics
- redaction before publishing
- local spool + retry + dead-letter
- deterministic sink routing

