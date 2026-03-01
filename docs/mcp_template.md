# MCP Integration Template for Other Python Repos

Use this checklist to replicate MCP bridge integration:

## 1. Copy modules

Copy `src/mcp_bridge/` and `src/mcp_bridge/template/` into target repo.

## 2. Add settings

Add `MCP_*` flags to runtime config:

- enabled/fail-open
- runtime config path
- spool path
- timeout/retry/backoff
- sink toggles and sink-specific IDs

## 3. Hook lifecycle events

At minimum publish:

- `run_started`
- `job_processing_started`
- `job_processing_completed`
- `job_processing_error`
- `fallback_triggered`
- `fallback_outcome`
- `human_handoff_started`
- `human_handoff_resolved`
- `human_handoff_timeout`
- `run_finished`

## 4. Add spool drain

- drain at startup
- drain at shutdown
- optional CLI `--mcp-drain-spool-only`

## 5. Add tests

- config parse
- redaction
- spool retry/dead-letter
- publisher routing using a fake local MCP server

