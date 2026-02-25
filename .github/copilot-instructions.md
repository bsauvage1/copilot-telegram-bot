---
description: 'Single-user Telegram bot bridging GitHub Copilot CLI via the Python SDK'
---

# Copilot Instructions

## Running the Bot

```bash
uv sync                # install dependencies
uv run main.py         # start the bot
```

After `uv sync`, fix the SDK binary permission if needed:
```bash
chmod +x ./.venv/lib/python3.*/site-packages/copilot/bin/copilot
```

There are no tests, linters, or CI pipelines configured.

## Architecture

Three-layer, event-driven design bridging the `github-copilot-sdk` (`CopilotClient` over JSON-RPC/stdio) with the Telegram Bot API (`python-telegram-bot` v20+ async).

- **`src/core/`** — SDK integration and state. `CopilotService` is the singleton orchestrator, composed via mixins: `EventHandlerMixin` (routes 12+ SDK event types) and `SessionMixin` (client lifecycle, permission bridge, model switching). `SessionContext` (`ctx`) is a global singleton holding shared state (working directory, tracked files). All state is in-memory — zero database.
- **`src/handlers/`** — Telegram handlers. Commands, chat messages, and inline-button callbacks. The permission bridge uses `asyncio.Future` objects: `messages.py` creates a Future + inline keyboard when the SDK requests tool approval, `callbacks.py` resolves it when the user taps Allow/Deny.
- **`src/ui/`** — Output formatting. `MessageSender` auto-splits at 4000 chars with safe code-block handling. `formatters.py` has per-tool display logic (bash, edit, create, grep, view, etc.). `menus.py` generates keyboard layouts and cockpit displays.

Entry point: `main.py` → `src/main.py` (registers all Telegram handlers and starts polling).

## Key Conventions

- **Python 3.10+, async/await throughout** — all handlers and service methods are async.
- **Package manager: `uv`** — dependencies in `pyproject.toml`, lockfile is `uv.lock`. Always use `uv run` to execute, `uv sync` to install.
- **Singleton pattern** — `CopilotService` is instantiated once as `service` at module level in `src/core/service.py`. `SessionContext` is instantiated once as `ctx` in `src/core/context.py`. Import these instances directly.
- **Mixin composition** — `CopilotService` inherits from `EventHandlerMixin` and `SessionMixin` to split SDK event routing and session lifecycle into separate files while sharing state via `self`.
- **Configuration via `.env`** — all config loaded in `src/config.py` via `python-dotenv`. Constants (timeouts, limits, default model) are also defined there.
- **Two-tier permission model** — tools in `_TOOL_ALLOWLIST` (in `session.py`) are auto-approved. All others (bash, edit, create) require user approval via Telegram inline keyboards.
- **Tool definitions use `@define_tool` + Pydantic models** — custom MCP tools in `src/core/tools.py` use the SDK's `@define_tool` decorator with Pydantic `BaseModel` for parameter validation. Each tool validates paths against `ctx.root_path` for workspace confinement.
- **`ContextVar` for per-task state** — `streaming_mode` in `src/core/context.py` uses `contextvars.ContextVar` so concurrent asyncio tasks each see their own value.
- **Telegram message limits** — `TELEGRAM_MSG_LIMIT` (4000 chars) is the safe ceiling. Messages are auto-split with code-fence tracking across chunks.
- **Logging** — standard `logging` module, one logger per file via `logging.getLogger(__name__)`.
- **Security checks** — every handler calls `security_check()` (verifies `ALLOWED_USER_ID`) and `check_project_selected()` before proceeding.
