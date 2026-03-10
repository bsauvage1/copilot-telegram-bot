import asyncio
import html
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any
from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from src.config import WORKSPACE_PATH
from src.core.service import service
from src.core.context import ctx
from src.core.instructions import (
    USER_INSTRUCTIONS_PATH,
    project_instructions_path,
    safe_read_instructions,
    EMPTY_FILE_SENTINEL,
    extract_summary,
)
from src.handlers.messages import chat_handler
from src.handlers.utils import security_check, check_project_selected
from src.ui.formatters import format_tokens, format_percentage, get_model_context_limit

logger = logging.getLogger(__name__)

_LATEST_CACHE_TTL_SECONDS = 3600
_LATEST_CACHE_ERROR_TTL_SECONDS = 60
_LATEST_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": None}


def _extract_version(version_text: str) -> str:
    match = re.search(r"(\d+\.\d+\.\d+)", version_text or "")
    return match.group(1) if match else "unknown"


def _is_prerelease(version_text: str) -> bool:
    """Return True if the raw version string contains a pre-release suffix."""
    match = re.search(r"\d+\.\d+\.\d+([._-]?\w+)", version_text or "")
    if not match:
        return False
    suffix = match.group(1)
    return bool(
        re.match(r"[._-]?(alpha|beta|rc|dev|pre|snapshot)", suffix, re.IGNORECASE)
    )


def _parse_version(version_text: str) -> tuple[int, int, int] | None:
    ver = _extract_version(version_text)
    if ver == "unknown":
        return None
    try:
        major, minor, patch = ver.split(".")
        return int(major), int(minor), int(patch)
    except Exception:
        return None


def _compare_versions(current: str, latest: str) -> str:
    cur = _parse_version(current)
    lat = _parse_version(latest)
    if not cur or not lat:
        return "unknown"
    if cur < lat:
        return "outdated"
    if cur > lat:
        return "ahead"
    # Same numeric triple — pre-release is older than stable
    if _is_prerelease(current) and not _is_prerelease(latest):
        return "outdated"
    return "current"


def _make_compare_url(repo: str, from_version: str, to_version: str) -> str | None:
    frm = _extract_version(from_version)
    to = _extract_version(to_version)
    if "unknown" in {frm, to} or frm == to:
        return None
    return f"https://github.com/{repo}/compare/v{frm}...v{to}"


def _fetch_json_sync(url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "copilot-telegram-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def _fetch_json(url: str, timeout: float = 5.0) -> dict:
    return await asyncio.to_thread(_fetch_json_sync, url, timeout)


_latest_versions_lock = asyncio.Lock()


async def _get_latest_versions() -> dict[str, str]:
    now = time.time()
    cached = _LATEST_CACHE.get("data")
    if cached and now < float(_LATEST_CACHE.get("expires_at", 0.0)):
        return cached

    async with _latest_versions_lock:
        # Re-check after acquiring lock — another coroutine may have refreshed
        cached = _LATEST_CACHE.get("data")
        if cached and time.time() < float(_LATEST_CACHE.get("expires_at", 0.0)):
            return cached

        latest = {
            "cli_latest": "unknown",
            "sdk_latest": "unknown",
            "cli_release_url": "https://github.com/github/copilot-cli/releases",
            "sdk_release_url": "https://github.com/github/copilot-sdk/releases",
        }

        cli_task = _fetch_json(
            "https://api.github.com/repos/github/copilot-cli/releases/latest"
        )
        sdk_release_task = _fetch_json(
            "https://api.github.com/repos/github/copilot-sdk/releases/latest"
        )
        sdk_pypi_task = _fetch_json("https://pypi.org/pypi/github-copilot-sdk/json")

        cli_json, sdk_release_json, sdk_pypi_json = await asyncio.gather(
            cli_task, sdk_release_task, sdk_pypi_task, return_exceptions=True
        )

        if isinstance(cli_json, dict):
            latest["cli_latest"] = _extract_version(cli_json.get("tag_name", ""))
            latest["cli_release_url"] = (
                cli_json.get("html_url") or latest["cli_release_url"]
            )

        if isinstance(sdk_release_json, dict):
            latest["sdk_latest"] = _extract_version(
                sdk_release_json.get("tag_name", "")
            )
            latest["sdk_release_url"] = (
                sdk_release_json.get("html_url") or latest["sdk_release_url"]
            )
        if latest["sdk_latest"] == "unknown" and isinstance(sdk_pypi_json, dict):
            latest["sdk_latest"] = _extract_version(
                sdk_pypi_json.get("info", {}).get("version", "")
            )
            latest["sdk_release_url"] = (
                "https://pypi.org/project/github-copilot-sdk/#history"
            )

        _LATEST_CACHE["data"] = latest
        got_real_data = (
            latest["cli_latest"] != "unknown" or latest["sdk_latest"] != "unknown"
        )
        ttl = (
            _LATEST_CACHE_TTL_SECONDS
            if got_real_data
            else _LATEST_CACHE_ERROR_TTL_SECONDS
        )
        _LATEST_CACHE["expires_at"] = time.time() + ttl
        return latest


_REPO_MAP = {
    "cli": "github/copilot-cli",
    "sdk": "github/copilot-sdk",
}


async def _fetch_whats_changed(component: str) -> str:
    """Fetch and aggregate 'What's Changed' sections from GitHub releases between current and latest."""
    s = await _get_version_snapshot()
    if component == "cli":
        current, latest = s["local_cli"], s["cli_latest"]
        label = "CLI"
    else:
        current, latest = s["sdk_installed"], s["sdk_latest"]
        label = "SDK"

    cur = _parse_version(current)
    lat = _parse_version(latest)
    if not cur or not lat or cur >= lat:
        return f"✅ {label} is already up to date ({current})."

    repo = _REPO_MAP.get(component, _REPO_MAP["cli"])
    try:
        releases = await _fetch_json(
            f"https://api.github.com/repos/{repo}/releases?per_page=50"
        )
    except Exception as e:
        logger.warning(f"Failed to fetch releases for {repo}: {e}")
        return "⚠️ Could not fetch release info from GitHub. Try again later."

    if not isinstance(releases, list):
        return "⚠️ Unexpected response from GitHub API."

    # Filter releases between current (exclusive) and latest (inclusive)
    relevant = []
    for rel in releases:
        tag = rel.get("tag_name", "")
        ver = _parse_version(_extract_version(tag))
        if ver and cur < ver <= lat:
            relevant.append(rel)

    if not relevant:
        return f"No releases found between {current} and {latest}."

    # Sort oldest → newest
    relevant.sort(
        key=lambda r: (
            _parse_version(_extract_version(r.get("tag_name", ""))) or (0, 0, 0)
        )
    )

    sections: list[str] = []
    for rel in relevant:
        tag = rel.get("tag_name", "")
        body = rel.get("body", "") or ""
        # Extract "What's Changed" section from markdown body
        changes = _extract_changes_section(body)
        if changes:
            sections.append(f"<b>{html.escape(tag)}</b>\n{changes}")
        else:
            # Fallback: use release name
            name = rel.get("name", tag)
            sections.append(f"<b>{html.escape(name)}</b>\n<i>(no changelog body)</i>")

    header = (
        f"📋 <b>{label} Changes: {html.escape(current)} → {html.escape(latest)}</b>\n"
    )
    return header + "\n\n".join(sections)


def _extract_changes_section(body: str) -> str:
    """Extract release body and convert GitHub Markdown to Telegram HTML."""
    # Strip HTML comments (e.g. generator footers)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()

    # Look for "What's Changed" or "## What's Changed" header
    pattern = (
        r"(?:^|\n)(?:#{1,3}\s*)?What'?s Changed\s*\n"
        r"(.*?)(?=\n#{1,3}\s|\n\*\*Full Changelog\*\*|\Z)"
    )
    m = re.search(pattern, body, re.DOTALL | re.IGNORECASE)
    section = m.group(1).strip() if m else body.strip()
    if not section:
        return ""

    # Convert fenced code blocks before line-by-line processing
    def _fenced_to_html(m: re.Match) -> str:
        code = html.escape(m.group(1).strip())
        return f"\n<pre><code>{code}</code></pre>\n"

    section = re.sub(r"```[^\n]*\n(.*?)```", _fenced_to_html, section, flags=re.DOTALL)

    lines = []
    in_pre = False
    for line in section.splitlines():
        # Pass pre/code blocks through verbatim
        if "<pre><code>" in line:
            in_pre = True
            lines.append(line)
            if "</code></pre>" in line:
                in_pre = False
            continue
        if in_pre:
            lines.append(line)
            if "</code></pre>" in line:
                in_pre = False
            continue
        line = line.strip()
        if not line or line.startswith("**Full Changelog**"):
            continue

        # Strip PR attribution and bare URLs before escaping
        line = re.sub(r"\s+by\s+@\S+\s+in\s+https?://\S+", "", line)
        line = re.sub(r"\s+in\s+https?://\S+", "", line)
        line = re.sub(r"https?://\S+", "", line).strip()
        if not line:
            continue

        # Headings: ### text → <b>text</b>
        heading_m = re.match(r"^#{1,3}\s+(.*)", line)
        if heading_m:
            lines.append(f"<b>{html.escape(heading_m.group(1).strip())}</b>")
            continue

        # Blockquotes: > text → <i>text</i> (skip generator footers)
        if line.startswith(">"):
            text = line.lstrip("> ").strip()
            text = re.sub(r"https?://\S+", "", text)
            text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text).strip()
            if text and not text.lower().startswith("generated by"):
                lines.append(f"<i>{html.escape(text)}</i>")
            continue

        # Bullet prefix
        line = re.sub(r"^[*\-]\s+", "• ", line)

        # Escape HTML special chars, then convert inline markdown
        line = html.escape(line)
        # **bold** → <b>bold</b>
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        # *italic* → <i>italic</i>  (single asterisk)
        line = re.sub(r"\*([^*\n]+?)\*", r"<i>\1</i>", line)
        # `code` → <code>code</code>
        line = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", line)
        # [link text](url) → link text
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)

        if line.strip() and line.strip() != "•":
            lines.append(line)

    return "\n".join(lines) if lines else ""


async def _get_version_snapshot() -> dict[str, Any]:
    cli_runtime = await service.get_cli_runtime_info(probe=True)
    installed_cli = cli_runtime["installed_version"] or "not found"
    used_cli = cli_runtime["active_version"] or "unknown"
    sdk_installed = _get_sdk_version()
    latest = await _get_latest_versions()

    cli_state = _compare_versions(used_cli, latest["cli_latest"])
    installed_cli_state = _compare_versions(installed_cli, latest["cli_latest"])
    sdk_state = _compare_versions(sdk_installed, latest["sdk_latest"])

    return {
        "local_cli": used_cli,
        "installed_cli": installed_cli,
        "used_cli": used_cli,
        "active_cli_source": cli_runtime["active_source"] or "unknown",
        "cli_fallback_reason": cli_runtime["fallback_reason"],
        "installed_cli_path": cli_runtime["installed_path"],
        "used_cli_path": cli_runtime["active_path"],
        "sdk_installed": sdk_installed,
        "cli_latest": latest["cli_latest"],
        "sdk_latest": latest["sdk_latest"],
        "cli_state": cli_state,
        "installed_cli_state": installed_cli_state,
        "sdk_state": sdk_state,
        "cli_release_url": latest["cli_release_url"],
        "sdk_release_url": latest["sdk_release_url"],
        "cli_compare_url": _make_compare_url(
            "github/copilot-cli", used_cli, latest["cli_latest"]
        ),
        "sdk_compare_url": _make_compare_url(
            "github/copilot-sdk", sdk_installed, latest["sdk_latest"]
        ),
    }


async def _build_versions_panel() -> tuple[str, InlineKeyboardMarkup]:
    s = await _get_version_snapshot()

    def _ver_line(label: str, installed: str, latest: str, state: str) -> str:
        inst = html.escape(installed)
        lat = html.escape(latest)
        if state == "current":
            return f"• {label}: {inst} ✅"
        if state == "outdated":
            return f"• {label}: {inst} · latest <b>{lat}</b> ⬆️"
        if state == "ahead":
            return f"• {label}: {inst} · latest {lat} 🔮"
        return f"• {label}: {inst} · latest {lat}"

    cli_state = s["cli_state"]
    sdk_state = s["sdk_state"]
    used_label = "CLI (used)"
    if s["active_cli_source"] in {"system", "bundled"}:
        used_label = f"CLI (used: {s['active_cli_source']})"

    lines = [
        "🧭 <b>Version Intel</b>",
        _ver_line(
            "CLI (installed)",
            s["installed_cli"],
            s["cli_latest"],
            s["installed_cli_state"],
        ),
        _ver_line(used_label, s["used_cli"], s["cli_latest"], cli_state),
        _ver_line("SDK", s["sdk_installed"], s["sdk_latest"], sdk_state),
    ]

    if s["active_cli_source"] == "bundled" and s["installed_cli"] != s["used_cli"]:
        lines.append("")
        lines.append(
            "⚠️ <b>Runtime fallback active:</b> installed CLI is incompatible "
            "with the current SDK, so the bundled CLI is being used instead."
        )

    # Upgrade commands (only when something needs action)
    cmds: list[str] = []
    if s["installed_cli_state"] == "outdated":
        cmds.append("<code>copilot update</code>")
    if sdk_state == "outdated":
        target = (
            html.escape(s["sdk_latest"]) if s["sdk_latest"] != "unknown" else "latest"
        )
        cmds.append(f'<code>uv add "github-copilot-sdk=={target}"</code>')
    if cmds:
        lines.append("")
        lines.append("📦 <b>Upgrade commands:</b>")
        lines.extend(f"  {c}" for c in cmds)
    else:
        lines.append("")
        lines.append("✅ Everything is up to date.")

    # Buttons: col1 = release notes, col2 = changes (conditional)
    buttons: list[list[InlineKeyboardButton]] = []

    # CLI row
    if s["cli_compare_url"]:
        buttons.append(
            [
                InlineKeyboardButton("📄 CLI Notes", url=s["cli_release_url"]),
                InlineKeyboardButton("📋 CLI Changes", callback_data="changelog:cli"),
            ]
        )
    else:
        buttons.append([InlineKeyboardButton("📄 CLI Notes", url=s["cli_release_url"])])

    # SDK row
    if s["sdk_compare_url"]:
        buttons.append(
            [
                InlineKeyboardButton("📄 SDK Notes", url=s["sdk_release_url"]),
                InlineKeyboardButton("📋 SDK Changes", callback_data="changelog:sdk"),
            ]
        )
    else:
        buttons.append([InlineKeyboardButton("📄 SDK Notes", url=s["sdk_release_url"])])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _get_sdk_version() -> str:
    """Get copilot SDK package version."""
    try:
        from importlib.metadata import version

        return version("github-copilot-sdk")
    except Exception:
        return "unknown"


# --- Handlers ---


async def build_start_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Build the project selector card for /start.

    Returns (selector_text, selector_keyboard).
    """
    from src.ui.menus import get_project_keyboard

    keyboard = get_project_keyboard(WORKSPACE_PATH)
    return "📂 Select a project below to begin.", keyboard


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("/start command received")
    if not await security_check(update):
        return
    selector_text, selector_kb = await build_start_menu()
    # Project selector card (deleted on selection)
    sel_msg = await update.message.reply_text(selector_text, reply_markup=selector_kb)
    context.user_data["start_message_id"] = sel_msg.message_id
    context.user_data["start_chat_id"] = sel_msg.chat_id
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    from src.ui.menus import get_help_content

    msg = get_help_content(project_selected=service.project_selected)
    await update.message.reply_text(msg)


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    report = await service.get_usage_report()
    await update.message.reply_text(report)


async def _mode_reply(update, emoji: str, label: str, rpc_ok: bool):
    """Send a mode-switch confirmation, noting if RPC was deferred.
    label must be a trusted string — callers must not pass user-supplied input directly."""
    suffix = "" if rpc_ok else " <i>(will apply on next session start)</i>"
    await update.message.reply_text(
        f"{emoji} {html.escape(label)}{suffix}", parse_mode="HTML"
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    context.user_data["plan_mode"] = False
    # Set directly (no RPC) — session is about to be torn down anyway
    service.agent_mode = "interactive"
    await service.reset_session()
    await update.message.reply_text("🧹 Session Cleared\nMemory reset.")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the currently processing request using SDK session.abort()."""
    if not await security_check(update):
        return
    if not service.session:
        await update.message.reply_text("⚠️ No active session.")
        return
    if not service._chat_lock.locked():
        await update.message.reply_text("ℹ️ No request in progress.")
        return
    try:
        service._cancelled = True
        await service.session.abort()
        await update.message.reply_text("🛑 Request cancelled.")
    except Exception as e:
        logger.error(f"Cancel failed: {e}")
        await update.message.reply_text(f"⚠️ Cancel failed: {e}")


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    context.user_data["plan_mode"] = False
    rpc_ok = await service.set_agent_mode("interactive")
    logger.info("Switched to Interactive mode")
    await _mode_reply(update, "💬", "Switched to Interactive (Edit) Mode", rpc_ok)


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    args = context.args
    if args:
        # /plan <prompt> — switch to plan mode and send prompt
        context.user_data["plan_mode"] = True
        rpc_ok = await service.set_agent_mode("plan")
        await _mode_reply(update, "📝", "Plan Mode ON", rpc_ok)
        prompt = " ".join(args)
        await chat_handler(update, context, override_text=prompt)
    else:
        # /plan with no args — toggle between plan and interactive
        currently_plan = context.user_data.get("plan_mode", False)
        if currently_plan:
            context.user_data["plan_mode"] = False
            rpc_ok = await service.set_agent_mode("interactive")
            await _mode_reply(
                update, "💬", "Switched to Interactive (Edit) Mode", rpc_ok
            )
        else:
            context.user_data["plan_mode"] = True
            rpc_ok = await service.set_agent_mode("plan")
            await _mode_reply(update, "📝", "Switched to Plan Mode", rpc_ok)


async def cwd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    cwd = service.get_working_directory()
    await update.message.reply_text(f"📂 Current working directory:\n{cwd}")


async def cockpit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the session cockpit — current model, mode, agent, MCP, workspace and stats."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    msg = await service.get_cockpit_message(context.user_data)
    await update.message.reply_text(msg)


async def _send_paged(message, text: str, header: str = "", max_msgs: int = 5):
    """Send text across multiple messages, capped at max_msgs, in preformatted blocks."""
    from src.config import TELEGRAM_MSG_LIMIT
    import html as html_lib

    chunk_size = TELEGRAM_MSG_LIMIT - 50  # leave room for <pre> tags and header
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    if len(chunks) > max_msgs:
        keep = chunk_size * max_msgs
        text = text[:keep]
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
        chunks[-1] += f"\n... truncated (showing {max_msgs}/{len(chunks)} pages)"
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        prefix = header if idx == 0 else f"(cont. {idx + 1}/{total})\n"
        safe = html_lib.escape(chunk)
        await message.reply_text(f"{prefix}<pre>{safe}</pre>", parse_mode="HTML")


async def ls_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Top-level only\n(no subfolders)", callback_data="ls:0:1"
                )
            ],
            [
                InlineKeyboardButton(
                    "🌿 Shallow\n(1 level deep)", callback_data="ls:1:1"
                )
            ],
            [
                InlineKeyboardButton(
                    "🌳 Full tree\n(2 levels deep)", callback_data="ls:2"
                )
            ],
        ]
    )
    await update.message.reply_text("Choose file tree view:", reply_markup=keyboard)


async def add_dir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add an extra directory to the session's accessible scope."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    path_arg = " ".join(context.args).strip() if context.args else ""
    if not path_arg:
        await update.message.reply_text(
            "Usage: /add_dir <path>\n"
            "Adds a directory to the agent's accessible scope (takes effect on next session reset).\n\n"
            "Example: /add_dir ~/dotfiles"
        )
        return

    path = Path(path_arg).expanduser().resolve()

    # Security: only allow directories within the user's home tree
    _home = Path.home().resolve()
    try:
        path.relative_to(_home)
    except ValueError:
        await update.message.reply_text(
            f"❌ Path must be within your home directory ({_home}).\nRejected: {path}"
        )
        return

    if not path.exists():
        await update.message.reply_text(f"❌ Path does not exist: {path}")
        return
    if not path.is_dir():
        await update.message.reply_text(f"❌ Not a directory: {path}")
        return

    path_str = str(path)
    if path_str in service.extra_dirs:
        await update.message.reply_text(f"ℹ️ Already added: {path_str}")
        return

    service.extra_dirs.append(path_str)
    await update.message.reply_text(
        f"✅ Added: {path_str}\n"
        f"Extra dirs: {len(service.extra_dirs)} total\n"
        "Use /reset to apply to a new session, or /list_dirs to see all."
    )


async def list_dirs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List the current project directory and any extra directories."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    cwd = service.session_info.cwd or str(ctx.root_path)
    lines = [f"📂 Project: {cwd}"]
    if service.extra_dirs:
        lines.append("\n➕ Extra directories:")
        for d in service.extra_dirs:
            lines.append(f"  • {d}")
        lines.append(
            "\nUse /reset to apply to a new session, or /remove_dir <path> to remove one."
        )
    else:
        lines.append("\nNo extra directories. Use /add_dir <path> to add one.")
    await update.message.reply_text("\n".join(lines))


async def remove_dir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove an extra directory from the session's accessible scope."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    path_arg = " ".join(context.args).strip() if context.args else ""
    if not path_arg:
        await update.message.reply_text("Usage: /remove_dir <path>")
        return

    path_str = str(Path(path_arg).expanduser().resolve())
    if path_str not in service.extra_dirs:
        await update.message.reply_text(
            f"❌ Not in extra dirs: {path_str}\nUse /list_dirs to see current list."
        )
        return

    service.extra_dirs.remove(path_str)
    await update.message.reply_text(
        f"✅ Removed: {path_str}\n"
        + (
            f"Remaining: {len(service.extra_dirs)} extra dir(s)."
            if service.extra_dirs
            else "No extra directories left."
        )
    )


async def context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    # Get current model context usage info
    if service.last_assistant_usage:
        usage = service.last_assistant_usage
        model_name = getattr(usage, "model", service.current_model or "Auto")

        # Get token information
        input_tokens = 0
        output_tokens = 0
        cache_tokens = 0

        # Try to get token info from last_assistant_usage or last_session_usage
        if hasattr(usage, "input_tokens"):
            input_tokens = int(usage.input_tokens or 0)
        if hasattr(usage, "output_tokens"):
            output_tokens = int(usage.output_tokens or 0)
        if hasattr(usage, "cache_read_tokens"):
            cache_tokens = int(usage.cache_read_tokens or 0)

        # Calculate totals and percentages (estimates — actual limits vary by model)
        total_used = input_tokens + output_tokens
        context_limit = get_model_context_limit(model_name)

        # Build message similar to the example
        total_pct = format_percentage(total_used, context_limit)
        system_pct = format_percentage(input_tokens, context_limit)
        messages_pct = format_percentage(output_tokens, context_limit)
        free_space = context_limit - total_used
        free_pct = format_percentage(free_space, context_limit)

        msg = (
            f"{model_name} · {format_tokens(total_used)}/{format_tokens(context_limit)} tokens ({total_pct})\n"
            f"Context (In):  {format_tokens(input_tokens)} ({system_pct})\n"
            f"Response (Out): {format_tokens(output_tokens)} ({messages_pct})\n"
            f"Free Space:    {format_tokens(free_space)} ({free_pct})\n"
        )

        if cache_tokens > 0:
            cache_pct = format_percentage(cache_tokens, context_limit)
            msg += f"Cached:        {format_tokens(cache_tokens)} ({cache_pct})\n"

        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(
            "📊 No usage data available yet. Send a message first."
        )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    from src.ui.menus import get_model_keyboard

    msg = await update.message.reply_text("🔄 Fetching models...")
    keyboard = get_model_keyboard(await service.get_available_models())
    await msg.edit_text("Select a model:", reply_markup=keyboard)


async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    msg = await update.message.reply_text("📤 Exporting session...")
    try:
        file_path = await service.export_session_to_file()
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                await update.message.reply_document(
                    document=f, filename=os.path.basename(file_path)
                )
            os.remove(file_path)
            await msg.delete()
        else:
            await msg.edit_text("⚠️ Failed to export session or empty.")
    except Exception as e:
        logger.error(f"Share failed: {e}")
        await msg.edit_text(f"⚠️ Error sharing session: {e}")


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show session info and workspace summary."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    # Fetch latest session metadata (name, created, modified) from list_sessions()
    await service.populate_session_metadata()

    session_info = service.get_session_info()
    tracker = service.usage_tracker

    # Session uptime from session_info
    uptime_str = session_info.duration()

    # Time since last bot restart (from in-process tracker)
    since_restart = (
        tracker._format_duration(time.time() - tracker.session_start_time)
        if tracker.session_start_time
        else "N/A"
    )

    model = service.user_selected_model or service.current_model or "Auto"
    mode = "Planning" if context.user_data.get("plan_mode") else "Chat"
    status = "Expired" if service.session_expired else "Active"

    # Full session ID from session_info
    session_id_full = session_info.session_id or service.session_id

    # Use created time from session_info (ISO format string from SDK)
    created_str = session_info.created or "N/A"

    # Use session_info fields
    cwd = session_info.cwd or str(ctx.root_path)
    branch = session_info.branch or "N/A"

    msg = f"📋 Session Info\n• Session ID: {session_id_full}\n• Status: {status}\n"
    if uptime_str and uptime_str != "N/A":
        msg += f"• Duration: {uptime_str}\n"
    msg += f"• Since restart: {since_restart}\n"
    if created_str and created_str != "N/A":
        msg += f"• Created: {created_str}\n"
    msg += f"• Model: {model}\n• Mode: {mode}\n"

    # Summary — prefer populated session_info.name, fall back to workspace.yaml
    from src.core.titler import (
        get_or_generate_summary,
        read_session_summary,
    )
    from src.ui.menus import _clean_summary

    raw_name = _clean_summary(session_info.name) if session_info.name else None
    summary = raw_name or read_session_summary(session_id_full or "")
    if not summary and session_id_full:
        summary = await get_or_generate_summary(session_id_full)
    if summary:
        msg += f"• Summary: {summary}\n"

    msg += (
        f"\n📂 Workspace\n"
        f"• Project: {service.project_name or Path(cwd).name}\n"
        f"• Path: {cwd}\n"
        f"• Branch: {branch}\n"
    )

    # Git root and repository if available
    if session_info.git_root:
        msg += f"• Git Root: {session_info.git_root}\n"
    if session_info.repository:
        msg += f"• Repository: {session_info.repository}\n"

    # Quota status
    quota_summary = tracker.get_quota_summary()
    if quota_summary:
        msg += f"\n💳 Quota Status:\n{quota_summary}\n\n"

    # Usage summary
    usage_summary = await tracker.get_usage_summary()
    msg += f"\n📊 Usage Summary:\n{usage_summary}\n"

    await update.message.reply_text(msg)


# ── New commands ──────────────────────────────────────────────────────────


async def diff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show git diff for the current project (paged, non-interactive)."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    cwd = service.get_working_directory()
    msg = await update.message.reply_text("🔍 Running git diff...")
    try:
        proc = await asyncio.create_subprocess_shell(
            "git diff HEAD",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        diff = stdout.decode().strip() or stderr.decode().strip()
        if not diff:
            await msg.edit_text("ℹ️ No uncommitted changes.")
            return
        # Chunk at line boundaries; HTML tags expand each line by ~10 chars, so keep raw budget at 2500
        chunks, current, current_len = [], [], 0
        for line in diff.splitlines():
            if current_len + len(line) + 1 > 2500 and current:
                chunks.append("\n".join(current))
                current, current_len = [line], len(line)
            else:
                current.append(line)
                current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        total = len(chunks)
        # Offer paging choice
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📄 1 page", callback_data="diff:1")],
                [InlineKeyboardButton("📄 3 pages", callback_data="diff:3")],
                [
                    InlineKeyboardButton(
                        f"📄 Full ({total} page{'s' if total != 1 else ''})",
                        callback_data=f"diff:{total}",
                    )
                ],
            ]
        )
        context.user_data["_diff_chunks"] = chunks
        await msg.edit_text(
            f"📋 Diff has {total} page(s). How much to show?",
            reply_markup=keyboard,
        )
    except asyncio.TimeoutError:
        await msg.edit_text("⚠️ Git diff timed out.")
    except Exception as e:
        logger.error(f"diff_command failed: {e}")
        await msg.edit_text(f"⚠️ Error: {e}")


async def instructions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View Copilot instructions — status panel showing active instructions with path and summary."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    cwd = service.get_working_directory()
    user_path = USER_INSTRUCTIONS_PATH
    project_path = project_instructions_path(cwd)

    entries = [
        ("👤 User", user_path, Path.home()),
        ("📁 Project", project_path, project_path.parent if project_path else None),
    ]
    status_lines: list[str] = []

    for label, path, allowed_root in entries:
        if path and path.is_file():
            raw = safe_read_instructions(path, allowed_root)
            if raw is not None and raw != EMPTY_FILE_SENTINEL:
                summary = extract_summary(raw)
                summary_line = f"\n   <i>{html.escape(summary)}</i>" if summary else ""
                path_line = f"\n   <code>{html.escape(str(path))}</code>"
                status_lines.append(
                    f"✅ <b>{label}</b>: active{path_line}{summary_line}"
                )
            elif raw == EMPTY_FILE_SENTINEL:
                status_lines.append(f"⚠️ <b>{label}</b>: file is empty")
            else:
                status_lines.append(
                    f"🚫 <b>{label}</b>: blocked (symlink outside allowed dir)"
                )
        else:
            path_str = html.escape(str(path)) if path else "n/a"
            status_lines.append(
                f"⚠️ <b>{label}</b>: not found — <code>{path_str}</code>"
            )

    msg = "📋 <b>Copilot Instructions</b>\n\n" + "\n".join(status_lines)
    await update.message.reply_text(msg, parse_mode="HTML")


async def versions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show machine/runtime versions, latest releases, and upgrade links."""
    if not await security_check(update):
        return
    text, keyboard = await _build_versions_panel()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update the Copilot CLI to the latest version."""
    if not await security_check(update):
        return
    import shutil

    # Try shutil.which first, then fall back to known install locations
    cli = (
        shutil.which("copilot")
        or getattr(service.client, "options", {}).get("cli_path")
        or os.path.expanduser("~/.local/bin/copilot")
        or "/usr/local/bin/copilot"
    )
    if not cli or not Path(cli).exists():
        await update.message.reply_text("⚠️ Copilot CLI not found.")
        return
    msg = await update.message.reply_text("🔄 Updating Copilot CLI...")
    try:
        proc = await asyncio.create_subprocess_exec(
            cli,
            "update",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = (stdout + stderr).decode().strip() or "Update complete."
        await msg.edit_text(f"✅ {output}"[:4000])
    except asyncio.TimeoutError:
        await msg.edit_text("⚠️ Update timed out (60s).")
    except Exception as e:
        logger.error(f"update_command failed: {e}")
        await msg.edit_text(f"⚠️ Update failed: {e}")


async def allow_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle allow-all-tools mode (skip per-request permission prompts). Alias: /yolo"""
    if not await security_check(update):
        return
    service.allow_all_tools = not service.allow_all_tools
    service.save_prefs()
    if service.allow_all_tools:
        await update.message.reply_text(
            "✅ Allow All Tools: ENABLED\n"
            "All tool permissions are auto-approved.\n"
            "Use /allow_all or /yolo again to restore prompts, or /reset_allowed_tools to disable."
        )
    else:
        await update.message.reply_text(
            "🔒 Allow All Tools: DISABLED\nPer-request permission prompts restored."
        )


async def yolo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /allow_all — toggle allow-all-tools mode."""
    await allow_all_command(update, context)


async def reset_allowed_tools_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Reset allowed tools — explicitly disables allow-all mode."""
    if not await security_check(update):
        return
    if not service.allow_all_tools:
        await update.message.reply_text(
            "ℹ️ Allow All Tools is already disabled — no change."
        )
        return
    service.allow_all_tools = False
    service.save_prefs()
    await update.message.reply_text(
        "🔒 Allowed Tools Reset.\nPer-request permission prompts restored."
    )


_MODE_LABELS = {
    "interactive": "💬 Interactive",
    "plan": "📋 Plan",
    "autopilot": "🚀 Autopilot",
}
_MODE_DESCRIPTIONS = {
    "interactive": "Standard chat — Copilot responds and waits for your next message.",
    "plan": "Planning only — Copilot outlines steps but won't execute tools or write files.",
    "autopilot": "Autonomous — Copilot chains steps and executes without waiting between actions.",
}


async def mcp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show MCP server status — enable/disable servers from mcp-config.json."""
    if not await security_check(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from src.core.mcp_config import (
        load_config,
        get_builtin_servers,
        MCP_CONFIG_PATH,
        query_tools,
    )

    msg = await update.message.reply_text("🔌 Loading MCP servers…")

    config = load_config()
    disabled = set(config.get("disabled", []))
    user_servers = config.get("mcpServers", {})
    builtins = get_builtin_servers()

    lines = ["🔌 <b>MCP Servers</b>\n"]

    lines.append("<b>Built-in (SDK managed):</b>")
    for name, srv in builtins.items():
        lines.append(f"  ✅ {html.escape(name)} ({html.escape(srv.get('type', '?'))})")

    lines.append("\n<b>User-configured:</b>")
    if not user_servers:
        lines.append("  None  —  edit ~/.copilot/mcp-config.json to add servers")
    else:
        enabled_servers = {n: s for n, s in user_servers.items() if n not in disabled}
        tool_results = await asyncio.gather(
            *[query_tools(srv) for srv in enabled_servers.values()],
            return_exceptions=True,
        )
        tools_by_name = dict(zip(enabled_servers.keys(), tool_results))

        for name, srv in user_servers.items():
            icon = "⬜" if name in disabled else "✅"
            kind = html.escape(srv.get("type", "?"))
            detail = html.escape(srv.get("url", srv.get("command", "")))
            lines.append(
                f"  {icon} <b>{html.escape(name)}</b> ({kind})  <code>{detail}</code>"
            )
            if name not in disabled:
                tools = tools_by_name.get(name)
                if isinstance(tools, list) and tools:
                    lines.append(f"    🔧 {html.escape(', '.join(tools))}")
                else:
                    lines.append("    🔧 (could not query tools)")

    lines.append(f"\n<code>{MCP_CONFIG_PATH}</code>")

    buttons = []
    for name in user_servers:
        label = f"{'▶️ Enable' if name in disabled else '⏸ Disable'} {name}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"mcp_toggle:{name}")]
        )
    buttons.append(
        [InlineKeyboardButton("🔄 Reload session now", callback_data="mcp_reload")]
    )

    await msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def autopilot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show mode picker: Interactive / Plan / Autopilot (maps to session.mode.set)."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Read current mode if session is active
    current = "interactive"
    if service.session:
        try:
            resp = await service.client._client.request(
                "session.mode.get", {"sessionId": service.session.session_id}
            )
            current = resp.get("mode", "interactive")
        except Exception:
            pass

    label = _MODE_LABELS.get(current, current)
    buttons = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if m == current else ''}{_MODE_LABELS[m]}",
                callback_data=f"mode:{m}",
            )
        ]
        for m in ("interactive", "plan", "autopilot")
    ]
    await update.message.reply_text(
        f"🤖 Agent Mode — currently: {label}\n\n"
        + "\n".join(
            f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _build_agent_picker(
    agents: list, current: str | None
) -> tuple[str, "InlineKeyboardMarkup"]:
    """Build the agent picker message text and keyboard."""
    import html as _html
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    current_label = _html.escape(
        next((a["name"] for a in agents if a["key"] == current), "Default")
        if current
        else "Default"
    )
    hint = "" if agents else "\n<i>No custom agents found in ~/.copilot/agents/</i>"
    buttons = [
        [
            InlineKeyboardButton(
                f"{'✅' if not current else '🤖'} Default",
                callback_data="agent:default",
            )
        ]
    ]
    for a in agents:
        icon = "✅" if a["key"] == current else a["icon"]
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{icon} {a['name']}", callback_data=f"agent_detail:{a['key']}"
                )
            ]
        )
    return f"🤖 <b>Select Agent:</b> {current_label}{hint}", InlineKeyboardMarkup(
        buttons
    )


async def agent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick a custom agent for the current session."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    from src.core.agents import get_available_agents

    agents = get_available_agents()
    text, keyboard = _build_agent_picker(agents, service.selected_agent)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set reasoning effort level for models that support it."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    model_id = service.user_selected_model or service.current_model
    if not model_id:
        await update.message.reply_text("⚠️ No model selected. Use /model first.")
        return
    # Populate cache if empty (e.g. first use before /model was called)
    if not service._models_cache:
        await service.get_available_models()
    model_info = next((m for m in service._models_cache if m["id"] == model_id), None)
    if not model_info or not model_info.get("supports_reasoning"):
        await update.message.reply_text(
            f"⚠️ Model '{model_id}' does not support reasoning effort.\n"
            "Switch to a reasoning-capable model with /model."
        )
        return
    from src.ui.menus import get_reasoning_keyboard

    current = (
        service.current_reasoning_effort
        or model_info.get("default_effort")
        or "default"
    )
    keyboard = get_reasoning_keyboard(
        model_id, model_info["supported_efforts"], model_info.get("default_effort")
    )
    await update.message.reply_text(
        f"🧠 Reasoning Effort — {model_id}\nCurrent: {current}\n\nSelect effort level:",
        reply_markup=keyboard,
    )


async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Browse and resume past Copilot sessions.

    Usage:
        /resume            — show session picker for current project
        /resume <id>       — directly resume the given session ID
    """
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    from src.ui.menus import get_sessions_keyboard
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    # Direct resume: /resume <session_id>
    if context.args:
        session_id = context.args[0].strip()
        msg = await update.message.reply_text(
            f"🔄 Resuming session {session_id[-8:]}..."
        )
        try:
            if not service._is_running:
                await service.start()
            await service.resume_session_by_id(session_id)
            from src.ui.menus import _clean_summary, _read_plan_summary
            from src.core.titler import read_session_summary

            summary = _clean_summary(read_session_summary(session_id))
            if not summary:
                summary = _read_plan_summary(session_id)
            if summary:
                await msg.edit_text(
                    f"✅ Session resumed: {session_id[-8:]}\n\n📝 {summary}"
                )
            else:
                await msg.edit_text(f"✅ Session resumed: {session_id[-8:]}")
        except Exception as e:
            logger.error(f"sessions_command direct resume failed: {e}")
            await msg.edit_text(f"⚠️ Failed to resume session: {e}")
        return

    msg = await update.message.reply_text("🔄 Fetching sessions...")
    try:
        if not service._is_running:
            await service.start()
        sessions = await service.client.list_sessions()
        if not sessions:
            await msg.edit_text("📋 No past sessions found.")
            return
        from src.core.titler import ensure_session_titles
        from src.ui.menus import get_visible_sessions

        cwd = service.get_working_directory()
        visible = get_visible_sessions(sessions, cwd_filter=cwd)
        await msg.edit_text("🔄 Fetching sessions... generating missing titles")
        await ensure_session_titles(visible)
        # Re-fetch so session objects carry the freshly written summaries
        sessions = await service.client.list_sessions()
        project_name = service.project_name or Path(cwd).name
        header, keyboard = get_sessions_keyboard(sessions, cwd_filter=cwd)
        buttons = list(keyboard.inline_keyboard)
        buttons.append(
            [
                InlineKeyboardButton(
                    "📋 Show more sessions",
                    callback_data="sessions_more",
                )
            ]
        )
        buttons.append(
            [InlineKeyboardButton("🌐 Show all projects", callback_data="sessions_all")]
        )
        await msg.edit_text(
            f"📋 Sessions for: {project_name}\n{header}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception as e:
        logger.error(f"sessions_command failed: {e}")
        await msg.edit_text(f"⚠️ Failed to fetch sessions: {e}")


async def infinite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle infinite sessions (automatic context compaction)."""
    if not await security_check(update):
        return
    service.infinite_sessions_enabled = not service.infinite_sessions_enabled
    service.save_prefs()
    if service.infinite_sessions_enabled:
        await update.message.reply_text(
            "♾️ Infinite Sessions: ENABLED\n"
            "Context auto-compaction is on. Takes effect on next /clear or session reset."
        )
    else:
        await update.message.reply_text(
            "🔒 Infinite Sessions: DISABLED\nManual context management restored."
        )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check Copilot CLI connection and auth status."""
    if not await security_check(update):
        return
    if not service._is_running:
        await update.message.reply_text(
            "🔴 Copilot CLI is not running. Select a project first."
        )
        return
    msg = await update.message.reply_text("🔄 Pinging...")
    try:
        state = service.client.get_state()
        ping = await service.client.ping()
        auth = await service.client.get_auth_status()
        login = auth.login or "unknown"
        authenticated = "✅" if auth.isAuthenticated else "❌"
        await msg.edit_text(
            f"🟢 Copilot CLI Status\n"
            f"• Connection: {state}\n"
            f"• Protocol: v{ping.protocolVersion}\n"
            f"• Auth: {authenticated} {login}\n"
            f"• Host: {auth.host or 'github.com'}"
        )
    except Exception as e:
        logger.error(f"ping_command failed: {e}")
        await msg.edit_text(f"🔴 Ping failed: {e}")


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hot-restart the Copilot CLI process, resuming the current session."""
    if not await security_check(update):
        return
    msg = await update.message.reply_text("🔄 Restarting Copilot CLI...")
    try:
        # Capture current session ID before stopping so we can resume it
        old_session_id = service.session.session_id if service.session else None
        await service.stop()
        # Re-create the client object pointing at the same CWD, then start fresh
        service.client = service._create_client(Path(service.get_working_directory()))
        await service.start()
        # Resume the old session to restore memory, falling back to new session
        if old_session_id:
            try:
                await service.resume_session_by_id(old_session_id)
                status = "• Session memory preserved ✅"
            except Exception as resume_err:
                logger.warning(f"Session resume failed after restart: {resume_err}")
                status = "• Session memory lost (resume failed) ⚠️"
        else:
            status = "• No prior session to resume"
        await msg.edit_text(
            "✅ Copilot CLI restarted\n"
            "• CLI process restarted\n"
            f"{status}\n"
            f"• Project: {service.project_name or 'none'}"
        )
    except Exception as e:
        logger.error(f"restart_command failed: {e}")
        await msg.edit_text(f"❌ Restart failed: {e}\nTry /start to reconnect.")


async def compact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compact context: resets session. Enable /infinite for auto-compaction."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    context.user_data["plan_mode"] = False
    # Set directly (no RPC) — session is about to be torn down anyway
    service.agent_mode = "interactive"
    await service.reset_session()
    tip = (
        ""
        if service.infinite_sessions_enabled
        else "\nTip: Use /infinite to enable automatic context compaction."
    )
    await update.message.reply_text(f"🗜️ Context compacted — session reset.{tip}")


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run an AI code review on the current git diff."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    cwd = service.get_working_directory()
    msg = await update.message.reply_text("🔍 Fetching diff for review...")
    try:
        proc = await asyncio.create_subprocess_shell(
            "git diff HEAD",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        diff = stdout.decode().strip()
    except Exception as e:
        await msg.edit_text(f"⚠️ Failed to get diff: {e}")
        return
    if not diff:
        await msg.edit_text("ℹ️ No uncommitted changes to review.")
        return
    await msg.delete()
    # Leave ~40k tokens for prompt overhead + response; 1 token ≈ 4 chars
    max_diff_chars = min(len(diff), 320_000)
    if len(diff) > max_diff_chars:
        truncation_note = f"\n\n⚠️ Diff truncated to {max_diff_chars:,} chars (full diff is {len(diff):,} chars)."
    else:
        truncation_note = ""
    prompt = (
        "Please review the following git diff. Focus on:\n"
        "- Bugs or logic errors\n"
        "- Security issues\n"
        "- Code quality and clarity\n"
        "- Any missing edge cases\n\n"
        f"```\n{diff[:max_diff_chars]}\n```"
        f"{truncation_note}"
    )
    await chat_handler(update, context, override_text=prompt)


async def changelog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate a changelog entry from recent git commits."""
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return
    cwd = service.get_working_directory()
    msg = await update.message.reply_text("🔍 Reading git log...")
    try:
        proc = await asyncio.create_subprocess_shell(
            "git log --oneline -30",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        log = stdout.decode().strip()
    except Exception as e:
        await msg.edit_text(f"⚠️ Failed to get git log: {e}")
        return
    if not log:
        await msg.edit_text("ℹ️ No commits found.")
        return
    await msg.delete()
    prompt = (
        "Generate a concise, well-formatted changelog entry based on these recent commits. "
        "Group changes by type (Features, Bug Fixes, Improvements). "
        "Use plain text bullet points.\n\n"
        f"Commits:\n{log}"
    )
    await chat_handler(update, context, override_text=prompt)


async def streamer_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle live streaming mode (real-time token display)."""
    if not await security_check(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    service.streaming_enabled = not service.streaming_enabled
    service.save_prefs()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Reset session now", callback_data="streamer:reset")]]
    )
    if service.streaming_enabled:
        await update.message.reply_text(
            "📡 Streamer Mode: ENABLED\n"
            "Responses will stream live as tokens arrive.\n"
            "A session reset is required for streaming to take effect.",
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            "🔇 Streamer Mode: DISABLED\n"
            "Responses will be sent as complete messages (default behavior).\n"
            "A session reset is required for the change to take effect.",
            reply_markup=keyboard,
        )


async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show skill directories and loaded skills — enable/disable individual skills."""
    if not await security_check(update):
        return
    from src.core.skills_config import scan_skills, get_disabled_skills
    from src.core.context import ctx
    from src.handlers.callbacks import build_skills_panel

    msg = await update.message.reply_text("🧩 Loading skills…")

    workspace = str(ctx.root_path) if ctx.root_path else None
    skills = scan_skills(workspace)
    disabled = set(get_disabled_skills())
    text, markup = build_skills_panel(skills, disabled, workspace)
    await msg.edit_text(text, parse_mode="HTML", reply_markup=markup)
