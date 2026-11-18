# github-copilot-sdk 2.0.0 upgrade audit

Audited 2026-11-18 against the `v2.0.0` tag of `github/copilot-sdk`
(`python/` sources, release notes). The Python wheel is not on PyPI yet;
PyPI latest is 1.0.34.

## Why 2.0.0 matters

CLI 1.0.113 speaks SDK protocol 4. Every 1.0.x SDK speaks protocol 3, so
the bot currently runs the SDK-pinned CLI (1.0.112 on SDK 1.0.34) through
the bundled-CLI fallback. SDK 2.0.0 requires protocol 4 and rejects 3.

## Already broken on 1.0.34 (independent of 2.0)

Verified live against SDK 1.0.34 on 2026-11-18.

| Where | Call | Problem |
|---|---|---|
| `src/core/service.py:598`, `src/core/session.py:775` | `session.get_messages()` | Removed; use `get_events()`. Export fails; session-info capture silently skipped. |
| `src/handlers/commands.py:1596` | `client.get_state()` | Removed. `/ping` fails. |
| `src/handlers/commands.py:1604` | `ping.protocolVersion` | Now `protocol_version`. |
| `src/ui/menus.py`, `src/handlers/callbacks.py`, `src/handlers/commands.py`, `src/core/session.py:465-472`, `src/core/titler.py:214` | `getattr(s, "sessionId"/"startTime"/"modifiedTime")` on `list_sessions()` results | `SessionMetadata` is snake_case (`session_id`, `start_time`, `modified_time`). `getattr` defaults hide it: local ids resolve to `None`, dates empty, local/remote dedupe broken. `RemoteSession` (ours) is still camelCase, so readers must handle both or `RemoteSession` should move to snake_case. |

## Breaking in 2.0.0

### Client construction (`src/core/service.py:_build_client_kwargs`)

- `CopilotClient(mode=...)` is now **required**: `"coding-agent"` for
  CLI-like defaults.
- Flat options are rejected with `TypeError`:
  - `working_directory=` moves to the runtime connection
    (`RuntimeConnection.for_spawned_stdio(path=..., working_directory=...)`)
    or per session (`create_session(working_directory=...)`).
  - `github_token=` becomes `github={"token": ...}`.
- `RuntimeConnection.for_stdio` is gone; use `for_spawned_stdio`.
- The default runtime is now `RuntimeConnection.for_default()`, which
  runs the native runtime **in-process** (`COPILOT_SDK_DEFAULT_CONNECTION`
  selects `stdio`/`inprocess`). The in-process transport rejects
  per-client `working_directory`.
- Consequence: the system-CLI-first / bundled-fallback logic in
  `service.py` (`_create_client`, `_discover_bundled_cli_path`,
  sticky fallback, `_probe_cli_compatibility`) and the `/versions`
  "used CLI" reporting should be redesigned, not just patched. Simplest
  path: rely on the SDK default runtime and drop the system CLI
  preference; cwd per session via `create_session(working_directory=...)`.
- `copilot.client._get_bundled_cli_path` stays gone; `_cli_download`
  still exists (`ensure_runtime_wrapper`, `get_cache_dir`,
  `get_runtime_platform`, `CLI_VERSION`).

### Session create/resume (`src/core/session.py` ~515-705, `src/core/titler.py:120`)

Flat kwargs raise `TypeError`. Mapping for what the bot passes:

| Today | 2.0.0 |
|---|---|
| `on_permission_request=` | `permissions={"on_request": ...}` |
| `on_user_input_request=` | `interaction={"on_user_input_request": ...}` |
| `hooks={"on_pre_tool_use": ..., "on_session_end": ...}` | `hooks={"handlers": {"on_pre_tool_use": ..., "on_session_end": ...}}` (flat keys raise "Unknown hooks") |
| `streaming=` | `events={"streaming": ...}` |
| `reasoning_effort=` | `inference={"reasoning_effort": ...}` |
| `mcp_servers=` | `mcp={"servers": ...}` |
| `skill_directories=` | `skills={"directories": ...}` |
| `disabled_skills=` | `skills={"disabled_skills": ...}` |
| `infinite_sessions=` | `state={"infinite_sessions": ...}` |
| `custom_agents=` | `agents={"definitions": ...}` |
| `tools=[]` (titler) | `tools={"definitions": []}` |
| `model=`, `system_message=`, `config_directory=` | unchanged (root level) |

### Response objects

- `GetAuthStatusResponse`: `isAuthenticated` → `is_authenticated`,
  `authType` → `auth_type`, `statusMessage` → `status_message`
  (`/ping`, `commands.py:1600`).
- Internal `copilot.generated` renamed to `copilot._generated`; the bot
  imports only `copilot.rpc` / `copilot.session_events`, which is fine.

### Still present (verified in v2.0.0 source)

`copilot.rpc.ToolsListRequest`, `PermissionDecisionApproveOnce`,
`PermissionDecisionReject`, `PermissionDecisionUserNotAvailable`,
`copilot.session.PermissionHandler.approve_all`,
`copilot.tools.define_tool(skip_permission=...)`, all 20
`SessionEventType` members the bot uses, `session.on/abort/disconnect/
set_model/send_and_wait(prompt, attachments=, timeout=)/get_events`,
`session.rpc.agent.list`, `client.rpc.tools.list`,
`client.list_sessions/list_models/get_status/get_auth_status/ping/
force_stop`, raw `session.mode.set`/`session.mode.get` RPC methods.

Risk: the bot calls `client._client.request("session.mode.set", ...)`
(5 sites). `_client` is private; check it still exists on the
in-process transport, or switch to `session.rpc.mode.set/get`.

## Behavior changes worth testing after upgrade

- `session.on()` handlers now fire in registration order. The bot
  registers `_handle_event` before `usage_tracker.handle_event`.
- Empty `tools=[]`, `custom_agents=[]`, `mcp_servers={}` are now sent
  instead of dropped; on **resume** they remove existing tools / agents /
  MCP servers.
- Resuming with a `commands` list replaces previously registered slash
  commands.
- Sending with an unavailable model now errors instead of silently
  picking another model. A stale `user_selected_model` in prefs will
  fail turns.
- Auto model selection no longer falls back to the legacy Auto API.
- `stop()` now times out session detach at 10 s and reports it as a stop
  error instead of hanging.
- `use_logged_in_user=False` ignores env tokens such as
  `COPILOT_GITHUB_TOKEN`; pass the token explicitly.
- External tool calls no longer time out after 30 minutes.

## Suggested upgrade order

1. Fix the 1.0.34 breakages above (independent, can ship now).
2. When 2.0.0 lands on PyPI: `uv add "github-copilot-sdk==2.0.0"`.
3. Rewrite `_build_client_kwargs` / `_create_client` (mode, runtime,
   github) and decide the CLI-selection design.
4. Regroup create/resume kwargs per the table; titler too.
5. Rename auth/ping fields in `/ping`.
6. Smoke test: start, project switch, resume from list, `/ping`,
   `/versions`, export, mode switch, permission prompt, user-input
   prompt, titler.
