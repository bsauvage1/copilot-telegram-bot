import logging
import re
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from src.config import WORKSPACE_PATH
from src.core.service import service
from src.handlers.messages import PENDING_INTERACTIONS
from src.handlers.utils import security_check

logger = logging.getLogger(__name__)

WAITING_PROJECT_NAME = 1


async def _refresh_auth_info(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh auth, CLI version and SDK version in context after service is started."""
    from src.handlers.commands import _get_system_info
    try:
        cli_version, auth, sdk_version = await _get_system_info()
        context.user_data['auth'] = auth
        context.user_data['cli_version'] = cli_version
        context.user_data['sdk_version'] = sdk_version
    except Exception as e:
        logger.debug(f"Auth/version refresh failed: {e}")


def _build_project_selected_message(context: ContextTypes.DEFAULT_TYPE, project_name: str, action: str = "Selected") -> str:
    """Build the edited start message shown after project selection/creation.
    
    Reuses version info stored in context.user_data by start_command.
    """
    auth = context.user_data.get('auth', 'Unknown')
    cli_version = context.user_data.get('cli_version', 'Unknown')
    sdk_version = context.user_data.get('sdk_version', 'Unknown')
    return (
        f"🚀 Copilot CLI-Telegram\n"
        f"User: {auth}\n"
        f"CLI version: {cli_version}\n"
        f"SDK version: {sdk_version}\n"
        f"✅ {action}: {project_name}"
    )


async def _build_project_selected_message_live(context: ContextTypes.DEFAULT_TYPE, project_name: str, action: str = "Selected") -> str:
    """Like _build_project_selected_message but re-fetches versions live (service is running)."""
    from src.handlers.commands import _get_system_info
    cli_version, auth, sdk_version = await _get_system_info()
    # Update cache so subsequent reads are consistent
    context.user_data['cli_version'] = cli_version
    context.user_data['auth'] = auth
    context.user_data['sdk_version'] = sdk_version
    return (
        f"🚀 Copilot CLI-Telegram\n"
        f"User: {auth}\n"
        f"CLI version: {cli_version}\n"
        f"SDK version: {sdk_version}\n"
        f"✅ {action}: {project_name}"
    )


async def _switch_project(path: Path, message, context: ContextTypes.DEFAULT_TYPE, query=None):
    """Common project-switching logic used by proj:, proj_granted:, and create_project_name.
    
    If `query` is provided (CallbackQuery), edits the original start message to remove the keyboard.
    """
    context.user_data['plan_mode'] = False
    await service.set_working_directory(str(path))

    # Edit the start message to remove inline keyboard and show final status
    if query:
        try:
            await _refresh_auth_info(context)
            selected_msg = _build_project_selected_message(context, path.name, "Selected")
            await query.edit_message_text(selected_msg)
        except Exception as e:
            logger.warning(f"⚠️ Failed to edit start message: {e}")

    cockpit = await service.get_cockpit_message(context.user_data)
    await message.reply_text(cockpit)


async def _handle_interaction_callback(query, update, context):
    """Handle perm: and input: callback queries."""
    parts = query.data.split(":")
    action_type = parts[0]
    interaction_id = parts[1]
    value = parts[2] if len(parts) > 2 else None

    logger.info(f"🔘 Button callback received | Type: {action_type} | ID: {interaction_id} | Value: {value}")

    interaction_data = PENDING_INTERACTIONS.get(interaction_id)

    if not interaction_data:
        logger.warning(f"⚠️ Interaction {interaction_id} not found in pending map")
        await query.edit_message_text("⚠️ Interaction expired or already handled.")
        return

    if isinstance(interaction_data, dict):
        future = interaction_data.get("future")
        options = interaction_data.get("options", [])
        logger.info(f"📦 Found interaction data | Future done: {future.done() if future else 'None'} | Options: {options}")
        if value and value.isdigit() and options:
            index = int(value)
            if 0 <= index < len(options):
                value = str(options[index])
                logger.info(f"🔄 Converted index {index} to option: {value}")
    else:
        future = interaction_data
        logger.warning(f"Found legacy future format for {interaction_id}")

    if future and not future.done():
        try:
            if action_type == "perm":
                result = (value == "allow")
                logger.info(f"✅ Resolving permission future with: {result}")
                future.set_result(result)
                # Extract tool name from stored interaction data
                tool_name = interaction_data.get("tool_name", "Tool") if isinstance(interaction_data, dict) else "Tool"
                action_emoji = "✓" if value == "allow" else "✕"
                action_text = "Allow" if value == "allow" else "Deny"
                decision_line = f"🛡️ Permission: {tool_name} → {action_text} {action_emoji}"
                await query.edit_message_text(decision_line)
            elif action_type == "input":
                logger.info(f"✅ Resolving input future with: {value}")
                future.set_result(value)
                await query.edit_message_text(f"❓ Selected: {value}")
                await query.message.reply_text(f"✅ Selected option: {value}")
            PENDING_INTERACTIONS.pop(interaction_id, None)
            logger.info(f"🧹 Cleaned up interaction {interaction_id}")
        except Exception as set_err:
            logger.error(f"❌ Error setting future result: {set_err}", exc_info=True)
            await query.edit_message_text(f"⚠️ Error processing selection: {str(set_err)}")
    else:
        logger.warning(f"⚠️ Future for {interaction_id} is None or already done")
        await query.edit_message_text("⚠️ Interaction expired or already handled.")


def _format_diff_chunk(raw_chunk: str) -> str:
    """Wrap a raw diff chunk in a <pre> code block for Telegram.

    Using <pre> gives a non-interactive monospace block (copy button, no link
    detection, consistent rendering). Inline bold/italic/strikethrough caused
    Telegram to interpret paths and identifiers as clickable links.
    """
    import html as html_lib
    return f"<pre>{html_lib.escape(raw_chunk)}</pre>"


async def _handle_diff_callback(query, context):
    """Handle diff: callback — send paged git diff with visual markup."""
    try:
        max_msgs = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.edit_message_text("⚠️ Invalid diff request. Run /diff again.")
        return
    chunks = context.user_data.get("_diff_chunks")
    if not chunks:
        await query.edit_message_text("⚠️ Diff data expired. Run /diff again.")
        return
    await query.delete_message()
    to_send = chunks[:max_msgs]
    remainder = len(chunks) - len(to_send)
    for i, chunk in enumerate(to_send):
        header = f"📋 Git Diff (page {i+1}/{len(to_send)}):\n" if len(to_send) > 1 else "📋 Git Diff:\n"
        text = header + _format_diff_chunk(chunk)
        if i == 0:
            await query.message.reply_text(text, parse_mode="HTML")
        else:
            await query.message.chat.send_message(text, parse_mode="HTML")
    if remainder > 0:
        await query.message.chat.send_message(
            f"ℹ️ {remainder} more page(s) not shown. Run /diff again to see more."
        )
    context.user_data.pop("_diff_chunks", None)


async def _handle_ls_callback(query, context):
    """Handle ls: callback — render file tree at chosen depth."""
    from src.core.filesystem import get_directory_listing, get_project_structure
    from src.handlers.commands import _send_paged
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    parts = query.data.split(":")
    depth = int(parts[1])
    max_msgs = int(parts[2]) if len(parts) > 2 else None

    # Full tree: ask for max messages first
    if depth == 2 and max_msgs is None:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 message\n(compact)", callback_data="ls:2:1")],
            [InlineKeyboardButton("3 messages\n(standard)", callback_data="ls:2:3")],
            [InlineKeyboardButton("5 messages\n(detailed)", callback_data="ls:2:5")],
        ])
        await query.edit_message_text("How much of the tree to show?", reply_markup=keyboard)
        return

    if max_msgs is None:
        max_msgs = 1

    await query.edit_message_text("Building tree...")
    if depth == 0:
        text = get_directory_listing(service.get_working_directory())
        header = "Top-level:\n"
    else:
        text = get_project_structure(service.get_working_directory(), max_depth=depth)
        header = "Shallow (1 level):\n" if depth == 1 else "Full tree (2 levels):\n"
    await query.delete_message()
    await _send_paged(query.message, text, header, max_msgs=max_msgs)


async def _handle_model_callback(query, context):
    """Handle model: callback queries."""
    model = query.data.split(":")[1]
    model_info = next((m for m in service._models_cache if m["id"] == model), None)
    if model_info and model_info.get("supports_reasoning") and model_info.get("supported_efforts"):
        from src.ui.menus import get_reasoning_keyboard
        keyboard = get_reasoning_keyboard(model, model_info["supported_efforts"], model_info.get("default_effort"))
        await query.edit_message_text(
            f"🤖 Model: {model}\n⚠️ Session will be reset (history cleared)\n\nSelect reasoning effort:",
            reply_markup=keyboard,
        )
    else:
        await service.change_model(model)
        await query.edit_message_text(f"✅ Model: {model} (⚠️ session reset)")


async def _handle_reasoning_callback(query, context):
    """Handle reasoning: callback queries."""
    parts = query.data.split(":")
    model = parts[1]
    effort = parts[2]

    if effort == "default":
        service.current_reasoning_effort = None
    else:
        service.current_reasoning_effort = effort

    await service.change_model(model, reasoning_effort=service.current_reasoning_effort)
    effort_display = effort.capitalize() if effort != "default" else "Default"
    await query.edit_message_text(
        f"✅ Model: {model} | Effort: {effort_display}\n⚠️ Session reset",
    )


async def _handle_session_callback(query, context):
    """Handle session: callback queries — resume a past session by ID."""
    from src.ui.menus import _clean_summary
    session_id = query.data.split(":", 1)[1]
    if session_id == "none":
        await query.answer("No sessions found for this project.", show_alert=True)
        return
    # Find the session object to get its summary before resuming
    session_obj = None
    try:
        sessions = await service.client.list_sessions()
        session_obj = next((s for s in sessions if getattr(s, 'sessionId', None) == session_id), None)
    except Exception:
        pass
    msg = await query.message.reply_text(f"🔄 Resuming session {session_id[-8:]}...")
    try:
        await service.resume_session_by_id(session_id)
        summary = _clean_summary(getattr(session_obj, 'summary', None)) if session_obj else None
        if summary:
            await msg.edit_text(f"✅ Session resumed: {session_id[-8:]}\n\n📝 Summary:\n{summary}")
        else:
            await msg.edit_text(f"✅ Session resumed: {session_id[-8:]}\n_(No summary available)_")
    except Exception as e:
        logger.error(f"Session resume failed: {e}")
        await msg.edit_text(f"⚠️ Failed to resume session: {e}")


async def _handle_sessions_all_callback(query, context):
    """Show sessions from all projects (no CWD filter)."""
    from src.ui.menus import get_sessions_keyboard
    await query.edit_message_text("🔄 Fetching all sessions...")
    try:
        sessions = await service.client.list_sessions()
        header, keyboard = get_sessions_keyboard(sessions, cwd_filter=None)
        await query.edit_message_text(f"📋 {header}", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"sessions_all failed: {e}")
        await query.edit_message_text(f"⚠️ Failed: {e}")


async def _handle_project_callback(query, context):
    """Handle proj: callback queries."""
    folder = query.data.split(":")[1]
    path = WORKSPACE_PATH / folder
    try:
        await _switch_project(path, query.message, context, query=query)
    except Exception as e:
        logger.error(f"Project Switch Failed: {e}")
        await query.message.reply_text(f"⚠️ Failed to switch project: {e}")


async def _handle_granted_project_callback(query, context):
    """Handle proj_granted: callback queries."""
    from src.config import GRANTED_PROJECT_PATHS
    try:
        idx = int(query.data.split(":")[1])
        if idx >= len(GRANTED_PROJECT_PATHS):
            await query.message.reply_text("⚠️ Invalid project index.")
            return
        path = GRANTED_PROJECT_PATHS[idx]
        if not path.exists():
            await query.message.reply_text(f"⚠️ Project path does not exist: {path}")
            return
        await _switch_project(path, query.message, context, query=query)
    except Exception as e:
        logger.error(f"Granted Project Switch Failed: {e}")
        await query.message.reply_text(f"⚠️ Failed to switch project: {e}")


def _mode_picker_header(active_mode: str) -> str:
    """Build the /autopilot picker header, noting permissions flavor when in autopilot."""
    from src.handlers.commands import _MODE_LABELS
    label = _MODE_LABELS.get(active_mode, active_mode)
    if active_mode == "autopilot":
        flavor = "all permissions" if service.allow_all_tools else "limited permissions"
        label += f" ({flavor})"
    return f"🤖 Agent Mode — currently: {label}"


async def _handle_mode_callback(query, context):
    """Handle /autopilot mode picker button taps."""
    from src.handlers.commands import _MODE_LABELS, _MODE_DESCRIPTIONS
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    mode = query.data.split(":", 1)[1]  # e.g. "mode:autopilot" → "autopilot"
    if not service.session:
        await query.edit_message_text("⚠️ No active session — select a project first.")
        return

    # Autopilot: show CLI-matching confirmation before activating
    if mode == "autopilot":
        buttons = [
            [InlineKeyboardButton("✅ Enable all permissions (recommended)", callback_data="autopilot_confirm:allow_all")],
            [InlineKeyboardButton("⚠️ Continue with limited permissions", callback_data="autopilot_confirm:limited")],
            [InlineKeyboardButton("❌ Cancel", callback_data="autopilot_confirm:cancel")],
        ]
        await query.edit_message_text(
            "🚀 <b>Enable Autopilot Mode</b>\n\n"
            "Autopilot mode works best with all permissions enabled. Without them, "
            "permission requests will be auto-denied and the agent may not complete "
            "tasks requiring file edits or shell commands.\n\n"
            "You can also enable permissions later with /allow_all",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    try:
        resp = await service.client._client.request(
            "session.mode.set",
            {"sessionId": service.session.session_id, "mode": mode}
        )
        active_mode = resp.get("mode", mode)
    except Exception as e:
        logger.error(f"mode.set failed: {e}")
        await query.edit_message_text("⚠️ Failed to set mode — check bot logs for details.")
        return

    # Keep service.agent_mode in sync; also sync plan_mode flag for footer display
    service.agent_mode = active_mode
    context.user_data['plan_mode'] = (active_mode == "plan")

    label = _MODE_LABELS.get(active_mode, active_mode)
    buttons = [
        [InlineKeyboardButton(
            f"{'✅ ' if m == active_mode else ''}{_MODE_LABELS[m]}",
            callback_data=f"mode:{m}"
        )]
        for m in ("interactive", "plan", "autopilot")
    ]
    await query.edit_message_text(
        _mode_picker_header(active_mode) + "\n\n"
        + "\n".join(f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_autopilot_confirm_callback(query, context):
    """Handle the autopilot permission confirmation step."""
    from src.handlers.commands import _MODE_LABELS, _MODE_DESCRIPTIONS
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    choice = query.data.split(":", 1)[1]  # allow_all | limited | cancel

    if choice == "cancel":
        # Restore the normal mode picker
        current = service.agent_mode
        label = _MODE_LABELS.get(current, current)
        buttons = [
            [InlineKeyboardButton(
                f"{'✅ ' if m == current else ''}{_MODE_LABELS[m]}",
                callback_data=f"mode:{m}"
            )]
            for m in ("interactive", "plan", "autopilot")
        ]
        await query.edit_message_text(
            _mode_picker_header(current) + "\n\n"
            + "\n".join(f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Set autopilot mode via RPC
    try:
        resp = await service.client._client.request(
            "session.mode.set",
            {"sessionId": service.session.session_id, "mode": "autopilot"}
        )
        active_mode = resp.get("mode", "autopilot")
    except Exception as e:
        logger.error(f"autopilot mode.set failed: {e}")
        await query.edit_message_text("⚠️ Failed to set Autopilot mode — check bot logs for details.")
        return

    service.agent_mode = active_mode
    context.user_data['plan_mode'] = False

    if choice == "allow_all":
        service.allow_all_tools = True
    else:
        service.allow_all_tools = False

    buttons = [
        [InlineKeyboardButton(
            f"{'✅ ' if m == active_mode else ''}{_MODE_LABELS[m]}",
            callback_data=f"mode:{m}"
        )]
        for m in ("interactive", "plan", "autopilot")
    ]
    await query.edit_message_text(
        _mode_picker_header(active_mode) + "\n\n"
        + "\n".join(f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_streamer_reset_callback(query, context):
    """Reset session when user taps the 'Reset session now' button in /streamer_mode."""
    context.user_data['plan_mode'] = False
    await service.reset_session()
    state = "ENABLED 📡" if service.streaming_enabled else "DISABLED 🔇"
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"✅ Session reset — Streamer Mode is now {state}.")


async def _handle_mcp_callback(query, context):
    """Handle mcp_toggle:<name> and mcp_reload callbacks."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from src.core.mcp_config import load_config, get_builtin_servers, toggle_server, MCP_CONFIG_PATH

    data = query.data

    if data == "mcp_reload":
        await service.reset_session()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🔄 Session reloaded — MCP servers re-applied.")
        return

    # mcp_toggle:<name>
    parts = data.split(":", 1)
    if len(parts) < 2 or not parts[1]:
        await query.answer("Invalid toggle request.", show_alert=True)
        return
    name = parts[1]
    now_enabled = toggle_server(name)
    status = "enabled ✅" if now_enabled else "disabled ⬜"

    # Redraw the panel
    config = load_config()
    disabled = set(config.get("disabled", []))
    user_servers = config.get("mcpServers", {})
    builtins = get_builtin_servers()

    lines = ["🔌 <b>MCP Servers</b>\n", "<b>Built-in (SDK managed):</b>"]
    for bname, srv in builtins.items():
        lines.append(f"  ✅ {bname} ({srv.get('type', '?')})")
    lines.append("\n<b>User-configured:</b>")
    for sname, srv in user_servers.items():
        icon = "⬜" if sname in disabled else "✅"
        kind = srv.get("type", "?")
        detail = srv.get("url", srv.get("command", ""))
        lines.append(f"  {icon} <b>{sname}</b> ({kind})  <code>{detail}</code>")
    lines.append(f"\n<i>{name} is now {status} — reload session to apply.</i>")
    lines.append(f"\n<code>{MCP_CONFIG_PATH}</code>")

    buttons = []
    for sname in user_servers:
        label = f"{'▶️ Enable' if sname in disabled else '⏸ Disable'} {sname}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"mcp_toggle:{sname}")])
    buttons.append([InlineKeyboardButton("🔄 Reload session now", callback_data="mcp_reload")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _apply_agent_selection(query, key: str) -> None:
    """Apply agent selection by key — shared by agent: and agent_select: routes."""
    import html as _html
    from src.core.agents import get_available_agents

    if not key:
        await query.answer("Invalid agent key.", show_alert=True)
        return

    agents = get_available_agents()

    if key == "default":
        service.selected_agent = None
        label = "Default"
        description = "Standard Copilot — all agents available for auto-inference."
    else:
        meta = next((a for a in agents if a["key"] == key), None)
        if not meta:  # key not in known agent list — reject
            await query.answer("Agent not found.", show_alert=True)
            return
        service.selected_agent = key
        label = _html.escape(meta["name"])
        description = _html.escape(meta.get("description", ""))

    await service.reset_session()
    await query.edit_message_text(
        f"🤖 <b>Agent:</b> {label}\n{description}\n\n⚠️ Session reset — ready to chat.",
        parse_mode="HTML",
    )


async def _handle_agent_callback(query, context):
    """Handle agent:<key> — apply selection."""
    key = query.data.split(":", 1)[1]
    await _apply_agent_selection(query, key)


async def _handle_agent_detail_callback(query, context):
    """Show detail card for an agent — edit picker in place."""
    import html as _html
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from src.core.agents import get_available_agents

    key = query.data.split(":", 1)[1]
    if not key:
        await query.answer("Invalid agent key.", show_alert=True)
        return
    agents = get_available_agents()
    meta = next((a for a in agents if a["key"] == key), None)
    if not meta:
        await query.answer("Agent not found.", show_alert=True)
        return

    is_current = service.selected_agent == key
    select_label = "✅ Already selected" if is_current else "✅ Select"
    await query.edit_message_text(
        f"{meta['icon']} <b>{_html.escape(meta['name'])}</b>\n\n{_html.escape(meta['description'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(select_label, callback_data=f"agent_select:{key}"),
            InlineKeyboardButton("◀️ Back", callback_data="agent_back"),
        ]]),
    )


async def _handle_agent_back_callback(query, context):
    """Return to the agent picker."""
    from src.core.agents import get_available_agents
    from src.handlers.commands import _build_agent_picker

    agents = get_available_agents()
    text, keyboard = _build_agent_picker(agents, service.selected_agent)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update): return
    logger.info(f"🎯 button_handler ENTRY - CallbackQuery received")

    query = update.callback_query
    logger.info(f"🎯 Query data: {query.data}")

    try:
        await query.answer()
    except Exception as e:
        logger.error(f"❌ query.answer() failed: {e}", exc_info=True)

    data = query.data

    try:
        if data.startswith("perm:") or data.startswith("input:"):
            await _handle_interaction_callback(query, update, context)
            return
        elif data.startswith("diff:"):
            await _handle_diff_callback(query, context)
        elif data.startswith("mode:"):
            await _handle_mode_callback(query, context)
        elif data.startswith("autopilot_confirm:"):
            await _handle_autopilot_confirm_callback(query, context)
        elif data.startswith("mcp_toggle:") or data == "mcp_reload":
            await _handle_mcp_callback(query, context)
        elif data.startswith("agent:"):
            await _handle_agent_callback(query, context)
        elif data.startswith("agent_detail:"):
            await _handle_agent_detail_callback(query, context)
        elif data.startswith("agent_select:"):
            await _apply_agent_selection(query, data.split(":", 1)[1])
        elif data == "agent_back":
            await _handle_agent_back_callback(query, context)
        elif data == "streamer:reset":
            await _handle_streamer_reset_callback(query, context)
        elif data.startswith("ls:"):
            await _handle_ls_callback(query, context)
        elif data == "sessions_all":
            await _handle_sessions_all_callback(query, context)
        elif data.startswith("model:"):
            await _handle_model_callback(query, context)
        elif data.startswith("reasoning:"):
            await _handle_reasoning_callback(query, context)
        elif data.startswith("session:"):
            await _handle_session_callback(query, context)
        elif data.startswith("proj_granted:"):
            await _handle_granted_project_callback(query, context)
            return ConversationHandler.END
        elif data.startswith("proj:"):
            await _handle_project_callback(query, context)
            return ConversationHandler.END
        elif data == "proj_new":
            context.user_data['start_message_id'] = query.message.message_id
            context.user_data['start_chat_id'] = query.message.chat_id
            await query.message.reply_text("New project name:")
            return WAITING_PROJECT_NAME
    except Exception as e:
        logger.error(f"❌ Error handling button callback '{data}': {e}", exc_info=True)
    return ConversationHandler.END


async def create_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update): return
    name = re.sub(r"[^\w-]+", "_", update.message.text).strip("_")
    if not name:
        await update.message.reply_text("⚠️ Invalid name. Try again or /cancel.")
        return WAITING_PROJECT_NAME
    path = WORKSPACE_PATH / name
    already_exists = path.exists()
    if already_exists:
        await update.message.reply_text(f"⚠️ Project {name} already exists. Switched to it.")
    else:
        path.mkdir(exist_ok=True)
        await update.message.reply_text(f"✅ Created: {name}")
    try:
        await _switch_project(path, update.message, context)
        # Hide the inline keyboard on the original /start message
        start_msg_id = context.user_data.pop('start_message_id', None)
        start_chat_id = context.user_data.pop('start_chat_id', None)
        if start_msg_id and start_chat_id:
            try:
                await _refresh_auth_info(context)
                action = "Selected" if already_exists else "Created"
                selected_msg = _build_project_selected_message(context, name, action)
                await context.bot.edit_message_text(
                    chat_id=start_chat_id,
                    message_id=start_msg_id,
                    text=selected_msg,
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to edit start message: {e}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error setting directory: {e}")
    return ConversationHandler.END


async def cancel_create_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel project creation and re-show the start menu with project keyboard."""
    if not await security_check(update): return
    logger.info("Project creation cancelled, returning to start menu")
    # Clean up stored message IDs
    context.user_data.pop('start_message_id', None)
    context.user_data.pop('start_chat_id', None)
    from src.handlers.commands import build_main_menu
    msg, keyboard, sys_info = await build_main_menu()
    context.user_data['cli_version'] = sys_info[0]
    context.user_data['auth'] = sys_info[1]
    context.user_data['sdk_version'] = sys_info[2]
    await update.message.reply_text(msg, reply_markup=keyboard)
    return ConversationHandler.END


async def reject_command_during_creation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject slash commands (other than /cancel) during project name input."""
    if not await security_check(update): return
    await update.message.reply_text("⚠️ Please enter a project name or use /cancel to go back.")
    return WAITING_PROJECT_NAME
