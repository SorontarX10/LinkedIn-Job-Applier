# MCP Runtime Setup

This project supports autonomous MCP publishing at runtime (without Codex tools).

## 1. Enable MCP in `.env`

Set:

```env
MCP_ENABLED=true
MCP_FAIL_OPEN=true
MCP_CONFIG_PATH=data/mcp_servers.runtime.json
MCP_SPOOL_PATH=data/mcp_spool.jsonl
MCP_PUBLISH_TIMEOUT_SEC=8
MCP_RETRY_LIMIT=5
MCP_RETRY_BACKOFF_SEC=20
MCP_REDACT_PII=true

MCP_LINEAR_ENABLED=true
MCP_LINEAR_TEAM=<TEAM_KEY_OR_ID>
MCP_LINEAR_PROJECT=<PROJECT_NAME_OR_ID>
MCP_LINEAR_DEFAULT_STATE=Backlog

MCP_NOTION_ENABLED=true
MCP_NOTION_DATA_SOURCE_ID=<COLLECTION_ID_OR_URL>
MCP_NOTION_PARENT_PAGE_ID=<OPTIONAL_PAGE_ID>

MCP_FIGMA_ENABLED=false
MCP_FIGMA_FILE_KEY=
```

## 2. Create runtime MCP config

Copy:

- `data/mcp_servers.runtime.example.json` -> `data/mcp_servers.runtime.json`

Then set command/args for your MCP servers.

## 3. Runtime behavior

- Publishing is best-effort.
- If MCP is unavailable, event is queued in local spool.
- Spool is drained automatically at run start/end.
- Use:

```powershell
python -m src.main --mcp-drain-spool-only
```

to force drain and exit.

## 4. Files

- `data/mcp_spool.jsonl`: append-only spool ops log
- `data/mcp_spool.dead_letter.jsonl`: events that exceeded retry limit
- `data/mcp_linear_issue_map.json`: dedupe map for Linear incidents
- `data/mcp_notion_run_map.json`: run_id -> Notion page map

## 5. Failure and recovery

If a sink fails:

1. event is spooled with pending target sinks
2. retry uses backoff (`MCP_RETRY_BACKOFF_SEC * attempt`)
3. after `MCP_RETRY_LIMIT`, event moves to dead-letter

