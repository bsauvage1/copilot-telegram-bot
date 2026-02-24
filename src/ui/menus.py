from pathlib import Path
from typing import List, Dict, Any, Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from src.config import WORKSPACE_PATH, GRANTED_PROJECT_PATHS


def _read_session_cwd(session_id: str) -> Optional[str]:
    """Read the cwd from ~/.copilot/session-state/<id>/workspace.yaml without PyYAML."""
    if not session_id:
        return None
    workspace = Path.home() / ".copilot" / "session-state" / session_id / "workspace.yaml"
    try:
        for line in workspace.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("cwd:"):
                return stripped[4:].strip() or None
    except Exception:
        pass
    return None


def _build_button_grid(items: List[InlineKeyboardButton], columns: int = 2) -> List[List[InlineKeyboardButton]]:
    """Build a grid of buttons with the given number of columns."""
    rows = []
    row = []
    for item in items:
        row.append(item)
        if len(row) == columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def get_project_keyboard(root_path: Path):
    if not root_path.exists():
        root_path.mkdir(parents=True, exist_ok=True)

    projects = []  # List of (name, callback_data) tuples

    # Add workspace subdirectories
    subdirs = sorted([d for d in root_path.iterdir() if d.is_dir() and not d.name.startswith('.')])
    for d in subdirs:
        projects.append((d.name, f"proj:{d.name}"))

    # Add granted projects
    for idx, granted_path in enumerate(GRANTED_PROJECT_PATHS):
        if granted_path.exists():
            projects.append((granted_path.name, f"proj_granted:{idx}"))

    projects.sort(key=lambda x: x[0].lower())

    btns = [InlineKeyboardButton(f"📂 {name}", callback_data=cb) for name, cb in projects]
    buttons = _build_button_grid(btns)
    buttons.append([InlineKeyboardButton("➕ Create New Project", callback_data="proj_new")])
    return InlineKeyboardMarkup(buttons)

_NOISE_PREFIXES = (
    "You are in GENERAL Mode",
    "You are in PLAN MODE",
    "You are assisting via a Telegram bot",
    "Please review the following git diff",
    "Generate a concise",
)

def _clean_summary(raw: str | None) -> str:
    """Return a display-friendly session summary, stripping system prompt noise."""
    if not raw:
        return "No summary"
    stripped = raw.strip()
    for prefix in _NOISE_PREFIXES:
        if stripped.startswith(prefix):
            return "—"
    return stripped[:38]


_PROJECT_ICONS = ["🔵", "🟢", "🟡", "🟠", "🔴", "🟣", "🟤", "⚪", "🔶", "🔷"]


def get_sessions_keyboard(sessions, cwd_filter: Optional[str] = None):
    """Build inline keyboard listing recent sessions for /sessions command.

    Returns (header_text, InlineKeyboardMarkup).

    If cwd_filter is provided, only sessions whose workspace.yaml cwd matches are shown.
    If cwd_filter is None, sessions from all projects are shown; each project gets a unique
    icon that appears both in the header legend and as a prefix on each session button.
    """
    sorted_sessions = sorted(
        sessions, key=lambda s: getattr(s, 'modifiedTime', '') or '', reverse=True
    )

    def _make_btn(s, session_id, icon=""):
        summary = _clean_summary(getattr(s, 'summary', None))
        start_time = getattr(s, 'startTime', None) or ""
        date_str = start_time[:10]
        time_str = start_time[11:16] if len(start_time) >= 16 else ""
        short_id = session_id[-8:] if len(session_id) > 8 else session_id
        prefix = f"{icon} " if icon else ""
        label = f"{prefix}{date_str} {time_str} [{short_id}]  {summary}"
        return InlineKeyboardButton(label, callback_data=f"session:{session_id}")

    if cwd_filter:
        # Single-project view: flat list filtered by CWD
        btns = []
        for s in sorted_sessions:
            if len(btns) >= 10:
                break
            session_id = getattr(s, 'sessionId', None) or str(s)
            if _read_session_cwd(session_id) != cwd_filter:
                continue
            btns.append(_make_btn(s, session_id))
        if not btns:
            btns.append(InlineKeyboardButton("No sessions for this project", callback_data="session:none"))
        return "Select a session to resume:", InlineKeyboardMarkup([[btn] for btn in btns])
    else:
        # All-projects view: assign icon per project, list legend in header
        from collections import defaultdict, OrderedDict
        groups: dict = OrderedDict()
        for s in sorted_sessions:
            session_id = getattr(s, 'sessionId', None) or str(s)
            cwd = _read_session_cwd(session_id)
            project = Path(cwd).name if cwd else "Unknown"
            if project not in groups:
                groups[project] = []
            groups[project].append((s, session_id))

        # Assign icons
        icon_map = {p: _PROJECT_ICONS[i % len(_PROJECT_ICONS)] for i, p in enumerate(groups)}

        # Build header legend
        legend = "\n".join(f"{icon_map[p]} {p}" for p in groups)
        header = f"All sessions by project:\n{legend}\n\nSelect to resume:"

        # Build buttons with icon prefix, up to 5 per project
        rows = []
        for project, items in groups.items():
            icon = icon_map[project]
            for s, session_id in items[:5]:
                rows.append([_make_btn(s, session_id, icon=icon)])

        if not rows:
            rows.append([InlineKeyboardButton("No sessions found", callback_data="session:none")])
        return header, InlineKeyboardMarkup(rows)


def get_model_keyboard(models_data: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    btns = []
    for m in models_data:
        m_id = m.get("id", "unknown")
        mult = m.get("multiplier", "1x")
        btns.append(InlineKeyboardButton(f"({mult}) {m_id}", callback_data=f"model:{m_id}"))
    buttons = _build_button_grid(btns)
    return InlineKeyboardMarkup(buttons)

def _command_reference() -> str:
    """Return the full command reference block."""
    return (
        "/start - Open project selection menu\n"
        "/help - Show help manual\n\n"
        "Core Workflow\n"
        "🤖 /model - Switch AI model\n"
        "💡 /effort - Set reasoning effort level\n"
        "⚙️ /autopilot - Mode picker: interactive / plan / autopilot\n"
        "📝 /plan - Switch to Plan Mode\n"
        "✏️ /edit - Switch to Interactive (Edit/Chat) Mode\n"
        "📋 /instructions - View Copilot instructions (user & project)\n"
        "🧠 /agent - Pick a custom agent (janitor, debug, security…)\n"
        "🔌 /mcp - View and enable/disable MCP servers\n"
        "🧩 /skills - View and enable/disable skills\n"
        "📡 /streamer_mode - Toggle live token streaming\n\n"
        "Session Control\n"
        "📂 /sessions - Browse & resume past sessions\n"
        "🗑️ /clear - Reset conversation memory\n"
        "📦 /compact - Compact context (smart reset)\n"
        "⛔ /cancel - Cancel in-progress request\n"
        "📤 /share - Export session to Markdown\n"
        "📊 /usage - Display session usage metrics\n"
        "🧮 /context - Display model context info\n"
        "ℹ️ /session - Show session info and workspace summary\n"
        "♾️ /infinite - Toggle infinite sessions (auto-compaction)\n"
        "🔓 /allow_all - Toggle allow-all-tools mode (alias: /yolo)\n"
        "🔒 /reset_allowed_tools - Disable allow-all and restore prompts\n\n"
        "Code Tools\n"
        "🔍 /diff - Show git diff (paged, monospace code block)\n"
        "🧐 /review - AI code review of current diff\n"
        "📜 /changelog - Generate changelog from git log\n\n"
        "Navigation\n"
        "📁 /ls - Project file tree\n"
        "📍 /cwd - Show current directory\n"
        "➕ /add_dir - Add extra directory to session scope\n"
        "📋 /list_dirs - List project + extra directories\n"
        "➖ /remove_dir - Remove an extra directory\n\n"
        "Utilities\n"
        "🎛️ /cockpit - Show session status (model, mode, agent, MCP…)\n"
        "🏓 /ping - Check CLI connection status\n"
        "⬆️ /update - Update Copilot CLI\n"
    )


def get_start_splash_content(auth_status: str, cli_version: str, sdk_version: str = "") -> str:
    """Minimal start splash — bot identity + project picker prompt. No commands."""
    sdk_line = f"SDK version: {sdk_version}\n" if sdk_version else ""
    return (
        f"🚀 Copilot CLI-Telegram\n"
        f"User: {auth_status}\n"
        f"CLI version: {cli_version}\n"
        f"{sdk_line}\n"
        "⚠️ Select a project below to begin."
    )


def get_cockpit_content(
    project_name: str,
    model: str,
    mode: str,
    path: str,
    branch: str,
    file_count: int,
    folder_count: int,
    effort: str = "",
    mcp_enabled: int = 0,
    mcp_total: int = 0,
    agent_name: str = "",
    agent_count: int = 0,
    streaming: bool = False,
    allow_all_tools: bool = False,
    extra_dirs: Optional[List[str]] = None,
    skills_enabled: int = 0,
    skills_total: int = 0,
    instructions_user: bool = False,
    instructions_project: bool = False,
) -> str:
    """Cockpit message sent after project selection — stats + status."""
    branch_line = f"🔀 Branch: {branch}\n" if branch else ""
    model_line = f"🤖 /model: {model}" + (f" [{effort}]" if effort else "") + "\n"
    mode_map = {"Chat": "interactive", "Plan": "plan", "Autopilot": "autopilot"}
    mode_value = mode_map.get(mode, mode.lower())
    mode_suffix = f" ({'full' if allow_all_tools else 'limited'} permissions)" if mode == "Autopilot" else ""
    mode_line = f"⚙️ /autopilot: {mode_value}{mode_suffix}\n"
    if mcp_total:
        mcp_line = f"🔌 /mcp: {mcp_enabled} active · {mcp_total} available\n"
    else:
        mcp_line = "🔌 /mcp: none\n"
    if skills_total:
        skills_line = f"🧩 /skills: {skills_enabled} active · {skills_total} available\n"
    else:
        skills_line = "🧩 /skills: none\n"
    instr_parts = (["user"] if instructions_user else []) + (["project"] if instructions_project else [])
    instructions_line = f"📋 /instructions: {' · '.join(instr_parts) if instr_parts else 'none'}\n"
    agent_label = agent_name if agent_name else "Default"
    agent_suffix = f" · {agent_count} available" if agent_count else ""
    agent_line = f"🧠 /agent: {agent_label}{agent_suffix}\n"
    streaming_line = f"📡 /streamer_mode: {'enabled' if streaming else 'disabled'}\n"
    if extra_dirs:
        dirs_lines = "\n".join(f"  • {d}" for d in extra_dirs)
        extra_dirs_line = f"➕ Extra dirs:\n{dirs_lines}\n"
    else:
        extra_dirs_line = "➕ Extra dirs: none\n"
    return (
        f"✅ Project Loaded: {project_name}\n\n"
        f"{model_line}"
        f"{mode_line}"
        f"{instructions_line}"
        f"{agent_line}"
        f"{mcp_line}"
        f"{skills_line}"
        f"{streaming_line}"
        f"📂 Workspace: {path}\n"
        f"{extra_dirs_line}"
        f"{branch_line}"
        f"📊 Stats: {file_count} files · {folder_count} folders\n\n"
        f"Type a message to start chatting, or /help for all commands."
    )


def get_help_content(project_selected: bool = False) -> str:
    """Help with status indicator and full command list."""
    status_dot = "🟢" if project_selected else "🔴"
    return (
        f"{status_dot} Copilot CLI-Telegram\n\n"
        f"{_command_reference()}"
        + ("" if project_selected else "\n⚠️ Action Required: Select or create a project to begin.")
    )


def get_reasoning_keyboard(model_id: str, supported_efforts: list, default_effort: str = None):
    """Build inline keyboard for reasoning effort selection."""
    effort_labels = {
        "low": "Low",
        "medium": "Medium", 
        "high": "High",
        "xhigh": "XHigh",
    }
    btns = []
    for effort in supported_efforts:
        label = effort_labels.get(effort, effort.capitalize())
        if default_effort and effort == default_effort:
            label += " (default)"
        btns.append(InlineKeyboardButton(label, callback_data=f"reasoning:{model_id}:{effort}"))
    buttons = _build_button_grid(btns)
    # Add skip button to use default
    buttons.append([InlineKeyboardButton("Skip (use default)", callback_data=f"reasoning:{model_id}:default")])
    return InlineKeyboardMarkup(buttons)
