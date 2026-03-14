# 🤖 copilot-telegram-bot

**Take GitHub Copilot anywhere on Telegram.**

Work from anywhere—coffee shops, transit, home—with real-time access to GitHub Copilot. This bot brings the Copilot CLI experience to Telegram. Built on the `github-copilot-sdk`, it's mobile-first, permission-aware, and security-focused.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![SDK](https://img.shields.io/badge/Copilot-SDK-black)
![Manager](https://img.shields.io/badge/uv-managed-purple)
![License](https://img.shields.io/badge/License-MIT-green.svg)

---

## ✨ Key Features

### 🤖 Three Operation Modes
- **📝 Plan Mode (Architecture):** Uses pre-defined, high-level prompts to help you brainstorm architecture, plan features, and outline project structures before writing a single line of code.
- **💬 Edit Mode (Development):** The standard Copilot experience. Write code, debug errors, run tests, and execute terminal commands directly from chat.
- **🚀 Autopilot Mode:** Full autonomous execution — Copilot runs tools and makes decisions without pausing for approval. Ideal for longer tasks where you want hands-free operation.

### 📱 Mobile-First UX
Forget typing long commands. We use **Telegram Inline Keyboards** for high-frequency actions:
- **Project Switcher:** Instantly switch between defined projects in your workspace via `/start`.
- **Interactive Permissions:** "Allow" or "Deny" tool execution (e.g., file writes, shell commands) with a single tap.
- **Smart Options:** When the model asks for clarification, reply via multiple-choice buttons.

### 👁️ Support Multimodal Vision
Don't just tell Copilot about the bug—**show it**.
- **Mockup to Code:** Send a photo of a whiteboard sketch and ask Copilot to generate the boilerplate.
- **Contextual Awareness:** Images are attached seamlessly to the prompt context.

### 📊 Developer-First HUD (Heads-Up Display)

**Cockpit** — after project selection, a status card shows the full session state:
>```
>✅ Project Loaded: webproject
>🤖 /model: gpt-4.1 [high]
>⚙️ /autopilot: interactive
>📋 /instructions: user · project
>🧠 /agent: Default · 5 built-in · 3 custom
>🔌 /mcp: 2 active · 4 available
>🧩 /skills: 1 active · 3 available
>📡 /streamer_mode: disabled
>📂 Workspace: /home/user/projects/webproject
>🔀 Branch: feature/auth
>📊 Stats: 42 files · 8 folders
>```

**Response footer** — every model reply is tagged with a compact context line and elapsed time:
>```
>🤖 gpt-4.1 [high] · ⚙️ interactive
>⏱ 4.2s
>```

**Tool events** — only **top-level tool calls** are displayed; child tool calls (those spawned inside subagents) are silently skipped to match CLI behavior. Subagent lifecycle (`started` / `completed`) is surfaced separately. Each tool type gets specialized formatting — bash commands show a command preview, file tools show the target path, `create` shows a content preview, and `update_todo` renders a checklist with status emojis. Long outputs are auto-truncated.

### 🛡️ Security & Control (Human-in-the-Loop)
- **Workspace Confinement:** Server-side enforcement of workspace paths. All file access restricted to `WORKSPACE_ROOT` + optional `GRANTED_PROJECTS` paths.
- **Two-Tier Permission Model:** Safe, read-only tools (`list_files`, `workspace_read_file`, `view`, `glob`, etc.) are **auto-approved** for seamless flow. Dangerous tools (`bash`, `edit`, `create`) require **explicit user approval** via inline buttons.
- **Transparent Tool Use:** Every tool invocation is displayed to you—auto-approved ones show inline, while dangerous ones pause and wait for your tap.
- **Chat Lock:** Prevents concurrent requests from interfering with each other.
- **Zero Database:** Lightweight, portable sessions stored in-memory—no persistence layer required.

## 🛠️ Prerequisites

1.  **Python 3.11+**
2.  **[uv](https://github.com/astral-sh/uv)** (Fast Python package manager)
3.  **GitHub Copilot CLI** (authenticated)
    ```bash
    npm install -g @github/copilot-cli
    copilot auth
    ```

## 🚀 Installation & Setup

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/julianchun/copilot-telegram-bot
    cd copilot-telegram-bot
    ```

2.  **Install Dependencies**
    ```bash
    uv sync
    ```
    
    > **⚠️ Important:** There may be a permission issue with the copilot SDK binary on v0.1.23. After running `uv sync`, fix the file permissions:
    > ```bash
    > chmod +x ./.venv/lib/python3.*/site-packages/copilot/bin/copilot
    > ```
    > Adjust the Python version (3.*) to match your environment.

3.  **Configuration**
    Create a `.env` file in the root directory:
    ```bash
    cp .env.example .env
    ```
    
    Edit `.env` with your details:
    ```env
    TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
    ALLOWED_USER_ID=your_telegram_user_id
    WORKSPACE_ROOT=/absolute/path/to/your/projects
    GRANTED_PROJECTS=/optional/additional/paths  # Comma-separated, optional
    GITHUB_TOKEN=ghp_your_github_token_here  # Optional, see below
    ```
    > **ALLOWED_USER_ID** is mandatory—only this user can access the bot. Get your ID from the first bot message if not set.
    
    > **WORKSPACE_ROOT** is your project sandbox. The bot cannot access files outside this directory (or `GRANTED_PROJECTS`).
    
    ### GitHub Authentication
    
    Two options for authenticating with GitHub Copilot:
    
    1. **GitHub CLI (default)**: Run `copilot auth` before starting the bot. The SDK will use your stored credentials.
    2. **GitHub Token (optional)**: Set `GITHUB_TOKEN` in `.env` to override CLI auth. This is useful for:
       - Containerized deployments where CLI auth is difficult
       - CI/CD environments
       - Programmatic access without interactive login
    
    If `GITHUB_TOKEN` is provided, it takes priority over CLI authentication. Otherwise, the bot relies on your existing `copilot auth` session.

4.  **Run the Bot**
    ```bash
    uv run main.py
    ```
    
    The bot will:
    - Print a startup splash with CLI version, SDK version, and auth status
    - Present a project selection keyboard
    - After project selection, show a cockpit with model, mode, branch, and file/folder stats
    - Auto-approve safe tools; prompt for dangerous tool permissions via inline buttons
    - Store all state in-memory—no database, pure Telegram state

## 🎮 Usage

### Main Menu (`/start`)
The bot launches into a **startup splash** showing:
- **Copilot CLI & SDK versions**
- **Authentication status**
- **Project selection keyboard** (2-column grid with a "Create project" button)

After selecting a project, a **cockpit message** appears with:
- Current model + billing multiplier
- Active mode (Interactive/Plan/Autopilot)
- Branch name + dirty status
- File/folder count in workspace

### Commands

| Command | Description |
| :--- | :--- |
| `/start` | Open the main dashboard and project selector. |
| `/help` | Show context-aware help with live status indicators. |
| `/plan` | Switch to **Plan Mode** (planning & architecture). Toggles back to Interactive with no args. |
| `/edit` | Switch to **Interactive (Edit/Chat) Mode**. |
| `/autopilot` | Agent mode picker: 💬 Interactive / 📋 Plan / 🚀 Autopilot. Uses native `session.mode.set`. |
| `/mcp` | View MCP servers from `~/.copilot/mcp-config.json`. Lists active tools dynamically via MCP protocol. Enable/disable per server. Reload session to apply. |
| `/skills` | View skills loaded from `~/.copilot/skills/` and `[project]/.github/skills/`. Enable/disable individual skills. Reload session to apply. |
| `/agent` | Pick a custom agent from `~/.copilot/agents/`. Compact picker with derived emoji icons. Tap an agent to see its description, then confirm or go back. Selecting one resets the session with that agent injected. Default = standard Copilot with all agents available for auto-inference. |
| `/model` | Hot-swap the underlying LLM (e.g., `gpt-4.1`). Shows billing multipliers. Reasoning effort picker for supported models. |
| `/effort` | Set reasoning effort directly (low / medium / high). |
| `/context` | Display model context and token usage info. |
| `/usage` | Display detailed session metrics — per-model token breakdown, cost, quota snapshots. |
| `/session` | Show session info and workspace summary. |
| `/resume` | Browse past Copilot sessions (filtered by project) and resume one. |
| `/share` | Export full session to Markdown file. |
| `/cancel` | Cancel an in-progress request. |
| `/tasks` | Show running background agents and shell tasks. |
| `/clear` | Reset conversation memory. |
| `/compact` | Compact context and reset session. |
| `/infinite` | Toggle infinite sessions (automatic context compaction). |
| `/allow_all` | Toggle (on/off) allow-all-tools mode — bypasses per-tool permission prompts. Alias: `/yolo`. |
| `/reset_allowed_tools` | Explicitly disable allow-all and restore per-tool permission prompts. |
| `/streamer_mode` | Toggle live token streaming — responses appear token-by-token in real time. Long responses auto-paginate. |
| `/ls` | List files in current directory with depth picker and paged output. |
| `/cwd` | Show current working directory. |
| `/add_dir` | Add an extra directory to the agent's accessible scope for the session (takes effect on next reset). |
| `/list_dirs` | List the project workspace and all extra directories added via `/add_dir`. |
| `/remove_dir` | Remove an extra directory from the session scope. |
| `/diff` | Show git diff for the current project (paged, monospace code block). |
| `/review` | Run an AI code review on the current git diff. |
| `/changelog` | Generate a changelog entry from recent git commits. |
| `/instructions` | View the `.github/copilot-instructions.md` file for the current project. |
| `/cockpit` | Re-display the session status card (model, mode, agent, MCP, workspace, branch, stats). |
| `/ping` | Check CLI connectivity and measure latency. |
| `/restart` | Restart the Copilot CLI process (hot-restart). Tears down and restarts the CLI binary, preserving your project selection and current session. Useful when the CLI is stuck or unresponsive. |
| `/versions` | Show machine/runtime/latest CLI+SDK versions with upgrade and release-note links. |
| `/update` | Update the Copilot CLI binary. |

## 🔧 Under the Hood
This bot is built on top of the **`github-copilot-sdk`**, which manages a `CopilotClient` process communicating via JSON-RPC over stdio.
- **Event-Driven:** Processes SDK events (`ASSISTANT_MESSAGE`, `TOOL_EXECUTION_START`, `SESSION_IDLE`, `SESSION_USAGE_INFO`) through an async queue bridge.
- **Session Lifecycle:** Manages session creation, expiration detection, context compaction, and automatic recovery—translating SDK states into Telegram interactions seamlessly.
- **Multimodal Encoding:** Encodes image attachments for the Copilot API, enabling visual reasoning capabilities.
- **Permission Bridge:** Intercepts tool invocations via `on_pre_tool_use` hook, routing dangerous operations through Telegram inline keyboards for human approval.

## ⚡ How Permissions Work

The bot uses a **two-tier permission model**:

**Auto-approved tools** (seamless, no interruption):
`list_files`, `workspace_read_file`, `view`, `glob`, `report_intent`, `task`, `update_todo`, `ask_user`, `fetch_copilot_cli_documentation`

**Requires explicit approval** (inline keyboard prompt):
`bash`, `edit`, `create`, and any other tool not in the allowlist.

**The Permission Flow** (for dangerous tools):

```
Agent wants to run a shell command (e.g., npm install)
          ↓
You see inline button: "[✅ Allow] [❌ Deny]"
          ↓
You click "Allow" directly in Telegram
          ↓
Agent executes and continues processing
```

Safe, read-only operations proceed automatically so you're not tapping "Allow" on every file read. Dangerous operations always pause and wait for your explicit approval.

**Permission Dialog Examples:**
- **Shell Command**: "🛡️ Permission request: **bash** with: `npm install` — Allow?"
- **File Write**: "🛡️ Permission request: **edit** with: `['src/app.ts']` — Allow?"
- **Model Selection**: Click `/model` → buttons appear → select your LLM → reasoning effort picker (for supported models) → switches instantly, history preserved
- **Agent Questions**: "❓ **Copilot Asks:** What's your preferred testing framework? [Jest] [Vitest] [Cancel]"

This keeps you **in the loop** on critical actions while maintaining a smooth flow for safe operations.

## 🏗️ Architecture

Three-layer, event-driven design under [src/](src/):

- **[src/main.py](src/main.py)**: Telegram bot entry point. Initializes handlers and polling.
  
- **[src/core/](src/core/)** — SDK & State Management:
  - **[service.py](src/core/service.py)**: `CopilotService` singleton. Manages high-level chat flow with 4 callbacks (`content_callback`, `status_callback`, `interaction_callback`, `completion_callback`).
  - **[session.py](src/core/session.py)**: `SessionMixin` — manages `CopilotClient` lifecycle, registers SDK event handlers, implements the **permission bridge** with tool allowlist + `on_pre_tool_use` hook.
  - **[events.py](src/core/events.py)**: SDK event dispatcher. Handles `ASSISTANT_MESSAGE`, `TOOL_EXECUTION_START/COMPLETE`, `SESSION_IDLE`, `SESSION_USAGE_INFO`, `SUBAGENT_STARTED/COMPLETED`, context compaction, and more.
  - **[context.py](src/core/context.py)**: `SessionContext` singleton — holds shared state (working directory, temp files, tracked files).
  - **[usage.py](src/core/usage.py)**: Per-model token/cost tracking, quota snapshots, session duration.
  - **[tools.py](src/core/tools.py)**: Read-only MCP tools (`list_files`, `workspace_read_file`) with strict path validation.
  - **[git.py](src/core/git.py)**: Branch detection and dirty-tree status for HUD footers.
  - **[filesystem.py](src/core/filesystem.py)**: Directory listing, project stats, noise-filtered file trees.

- **[src/handlers/](src/handlers/)** — Telegram Handlers:
  - **[commands.py](src/handlers/commands.py)**: All 24 bot commands (`/start`, `/help`, `/plan`, `/model`, `/diff`, `/review`, `/resume`, etc.).
  - **[messages.py](src/handlers/messages.py)**: Chat messages + file attachments. Implements interaction callback — when agent needs permission, creates `asyncio.Future` + inline keyboard.
  - **[callbacks.py](src/handlers/callbacks.py)**: Inline button clicks. Resolves Futures — when user taps "Allow"/"Deny", resolves `future.set_result()`.

- **[src/ui/](src/ui/)** — Output & Formatting:
  - **[streamer.py](src/ui/streamer.py)**: `MessageSender` — sends tool events as permanent messages, final response with footer. Auto-splits at 4000 chars, handles code block safety, retry on rate limits. Also drives `/streamer_mode` live-edit streaming with a rolling 3800-char preview window and proper multi-page finalization.
  - **[formatters.py](src/ui/formatters.py)**: Specialized tool display (bash, edit, create, grep, view, report_intent, task, update_todo) with heredoc truncation.
  - **[menus.py](src/ui/menus.py)**: Menu text generation, inline keyboard layouts, cockpit display.
  - **[session_exporter.py](src/ui/session_exporter.py)**: Exports full sessions to formatted Markdown files.

## ⚠️ Limitations
- **Single-user only** — designed for personal use with one `ALLOWED_USER_ID`
- **Requires Copilot subscription** — a GitHub Copilot Individual, Business, or Enterprise plan is required
- **Project switching restarts session** — the SDK requires a fresh `CopilotClient` process per working directory, so switching projects clears conversation history


## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
