import logging
import re
from pathlib import Path
from telegram import Update, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from src.config import WORKSPACE_PATH
from src.core.service import service
from src.handlers.messages import PENDING_INTERACTIONS
from src.handlers.utils import security_check

logger = logging.getLogger(__name__)

WAITING_PROJECT_NAME = 1


def _project_switch_already_succeeded(path: Path) -> bool:
    """Return True when the project switch succeeded despite a late UI error."""
    try:
        return (
            Path(service.get_working_directory()).resolve() == path.resolve()
            and service.project_selected
            and service.session is not None
        )
    except Exception:
        return False


async def _safe_reset_session(query) -> bool:
    """Reset session only if no chat is in flight. Returns False and alerts user if busy."""
    if service._chat_lock.locked():
        await query.answer(
            "⏳ A request is in progress — please wait.", show_alert=True
        )
        return False
    await service.reset_session()
    return True


async def _switch_project(
    path: Path, message, context: ContextTypes.DEFAULT_TYPE, query=None
):
    """Common project-switching logic used by proj:, proj_granted:, and create_project_name."""
    context.user_data["plan_mode"] = False
    await service.set_working_directory(str(path))

    # Delete the project selector card
    if query:
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"⚠️ Failed to delete selector card: {e}")

    # Versions card (service is now running with correct CWD)
    from src.handlers.commands import _build_versions_panel

    text, keyboard = await _build_versions_panel()
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    # Cockpit card
    cockpit = await service.get_cockpit_message(context.user_data)
    await message.reply_text(cockpit)

    warning = service.pop_pending_runtime_warning()
    if warning:
        await message.reply_text(warning, parse_mode="HTML")


async def _handle_interaction_callback(query, update, context):
    """Handle perm: and input: callback queries."""
    parts = query.data.split(":")
    action_type = parts[0]
    interaction_id = parts[1]
    value = parts[2] if len(parts) > 2 else None

    logger.info(
        f"🔘 Button callback received | Type: {action_type} | ID: {interaction_id} | Value: {value}"
    )

    interaction_data = PENDING_INTERACTIONS.get(interaction_id)

    if not interaction_data:
        logger.warning(f"⚠️ Interaction {interaction_id} not found in pending map")
        await query.edit_message_text("⚠️ Interaction expired or already handled.")
        return

    if isinstance(interaction_data, dict):
        future = interaction_data.get("future")
        options = interaction_data.get("options", [])
        logger.info(
            f"📦 Found interaction data | Future done: {future.done() if future else 'None'} | Options: {options}"
        )
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
                # Resolve with a Literal string so _on_permission_request
                # can use exhaustive equality checks instead of truthy tests.
                if value == "allow_session":
                    result: str = "allow_session"
                    action_emoji = "✓✓"
                    action_text = "Allow (session)"
                elif value == "allow":
                    result = "allow"
                    action_emoji = "✓"
                    action_text = "Allow"
                else:
                    result = "deny"
                    action_emoji = "✕"
                    action_text = "Deny"
                logger.info(f"✅ Resolving permission future with: {result}")
                future.set_result(result)
                # Extract tool name from stored interaction data
                tool_name = (
                    interaction_data.get("tool_name", "Tool")
                    if isinstance(interaction_data, dict)
                    else "Tool"
                )
                decision_line = (
                    f"🛡️ Permission: {tool_name} → {action_text} {action_emoji}"
                )
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
            await query.edit_message_text(
                f"⚠️ Error processing selection: {str(set_err)}"
            )
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
        header = (
            f"📋 Git Diff (page {i + 1}/{len(to_send)}):\n"
            if len(to_send) > 1
            else "📋 Git Diff:\n"
        )
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
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("1 message\n(compact)", callback_data="ls:2:1")],
                [
                    InlineKeyboardButton(
                        "3 messages\n(standard)", callback_data="ls:2:3"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "5 messages\n(detailed)", callback_data="ls:2:5"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            "How much of the tree to show?", reply_markup=keyboard
        )
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
    if (
        model_info
        and model_info.get("supports_reasoning")
        and model_info.get("supported_efforts")
    ):
        from src.ui.menus import get_reasoning_keyboard

        keyboard = get_reasoning_keyboard(
            model, model_info["supported_efforts"], model_info.get("default_effort")
        )
        await query.edit_message_text(
            f"🤖 Model: {model}\n\nSelect reasoning effort:",
            reply_markup=keyboard,
        )
    else:
        await service.change_model(model)
        await query.edit_message_text(f"✅ Model: {model}")


async def _handle_reasoning_callback(query, context):
    """Handle reasoning: callback queries."""
    parts = query.data.split(":")
    model = parts[1]
    effort = parts[2]
    reasoning_effort = None if effort == "default" else effort

    await service.change_model(model, reasoning_effort=reasoning_effort)
    warning = service.pop_pending_runtime_warning()
    if warning:
        await query.edit_message_text(warning, parse_mode="HTML")
        return
    effort_display = effort.capitalize() if effort != "default" else "Default"
    await query.edit_message_text(
        f"✅ Model: {model} | Effort: {effort_display}\n"
        "⚠️ History cleared (new session required for reasoning effort change).",
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
        session_obj = next(
            (s for s in sessions if getattr(s, "sessionId", None) == session_id), None
        )
    except Exception:
        pass
    msg = await query.message.reply_text(f"🔄 Resuming session {session_id[-8:]}...")
    try:
        await service.resume_session_by_id(session_id)
        summary = (
            _clean_summary(getattr(session_obj, "summary", None))
            if session_obj
            else None
        )
        if not summary:
            from src.ui.menus import _read_plan_summary

            summary = _read_plan_summary(session_id)
        if summary:
            await msg.edit_text(
                f"✅ Session resumed: {session_id[-8:]}\n\n📝 Summary:\n{summary}"
            )
        else:
            await msg.edit_text(f"✅ Session resumed: {session_id[-8:]}")
    except Exception as e:
        logger.error(f"Session resume failed: {e}")
        await msg.edit_text(f"⚠️ Failed to resume session: {e}")


async def _handle_sessions_all_callback(query, context):
    """Show sessions from all projects (no CWD filter)."""
    from src.ui.menus import get_sessions_keyboard, get_visible_sessions
    from src.core.titler import ensure_session_titles

    await query.edit_message_text("🔄 Fetching all sessions...")
    try:
        sessions = await service.client.list_sessions()
        visible = get_visible_sessions(sessions, cwd_filter=None)
        await query.edit_message_text(
            "🔄 Fetching all sessions... generating missing titles"
        )
        await ensure_session_titles(visible)
        # Re-fetch so session objects carry the freshly written summaries
        sessions = await service.client.list_sessions()
        header, keyboard = get_sessions_keyboard(sessions, cwd_filter=None)
        await query.edit_message_text(f"📋 {header}", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"sessions_all failed: {e}")
        await query.edit_message_text(f"⚠️ Failed: {e}")


async def _handle_sessions_more_callback(query, context, page: int = 0):
    """Show paginated button-list of recent sessions."""
    from src.ui.menus import get_sessions_text_page, _SESSIONS_MORE_PAGE_SIZE
    from src.core.titler import ensure_session_titles

    await query.edit_message_text("🔄 Loading sessions...")
    try:
        sessions = await service.client.list_sessions()
        sorted_sessions = sorted(
            sessions,
            key=lambda s: getattr(s, "modifiedTime", "") or "",
            reverse=True,
        )
        page_size = _SESSIONS_MORE_PAGE_SIZE
        start = page * page_size
        visible = sorted_sessions[start : start + page_size]
        await ensure_session_titles(visible)
        sessions = await service.client.list_sessions()
        text, keyboard = get_sessions_text_page(sessions, page=page)
        await query.edit_message_text(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"sessions_more failed: {e}")
        await query.edit_message_text(f"⚠️ Failed: {e}")


async def _handle_project_callback(query, context):
    """Handle proj: callback queries."""
    folder = query.data.split(":")[1]
    path = (WORKSPACE_PATH / folder).resolve()
    # Security: validate the resolved path stays within WORKSPACE_PATH
    try:
        path.relative_to(WORKSPACE_PATH.resolve())
    except ValueError:
        logger.warning(f"⚠️ Project traversal blocked: {folder!r} resolved to {path}")
        await query.message.reply_text("⚠️ Invalid project path.")
        return
    if not path.is_dir():
        await query.message.reply_text("⚠️ Project directory not found.")
        return
    try:
        await _switch_project(path, query.message, context, query=query)
    except Exception as e:
        if _project_switch_already_succeeded(path):
            logger.warning(f"Suppressing late project-switch warning: {e}")
            return
        logger.error(f"Project Switch Failed: {e}")
        await query.message.reply_text(f"⚠️ Failed to switch project: {e}")


async def _handle_granted_project_callback(query, context):
    """Handle proj_granted: callback queries."""
    from src.config import GRANTED_PROJECT_PATHS

    path = None
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
        if path is not None and _project_switch_already_succeeded(path):
            logger.warning(f"Suppressing late granted-project warning: {e}")
            return
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
            [
                InlineKeyboardButton(
                    "✅ Enable all permissions (recommended)",
                    callback_data="autopilot_confirm:allow_all",
                )
            ],
            [
                InlineKeyboardButton(
                    "⚠️ Continue with limited permissions",
                    callback_data="autopilot_confirm:limited",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel", callback_data="autopilot_confirm:cancel"
                )
            ],
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
            "session.mode.set", {"sessionId": service.session.session_id, "mode": mode}
        )
        active_mode = resp.get("mode", mode)
    except Exception as e:
        logger.error(f"mode.set failed: {e}")
        await query.edit_message_text(
            "⚠️ Failed to set mode — check bot logs for details."
        )
        return

    # Keep service.agent_mode in sync; also sync plan_mode flag for footer display
    service.agent_mode = active_mode
    context.user_data["plan_mode"] = active_mode == "plan"
    service.save_prefs()

    buttons = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if m == active_mode else ''}{_MODE_LABELS[m]}",
                callback_data=f"mode:{m}",
            )
        ]
        for m in ("interactive", "plan", "autopilot")
    ]
    await query.edit_message_text(
        _mode_picker_header(active_mode)
        + "\n\n"
        + "\n".join(
            f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS
        ),
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
        buttons = [
            [
                InlineKeyboardButton(
                    f"{'✅ ' if m == current else ''}{_MODE_LABELS[m]}",
                    callback_data=f"mode:{m}",
                )
            ]
            for m in ("interactive", "plan", "autopilot")
        ]
        await query.edit_message_text(
            _mode_picker_header(current)
            + "\n\n"
            + "\n".join(
                f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS
            ),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Set autopilot mode via RPC
    try:
        resp = await service.client._client.request(
            "session.mode.set",
            {"sessionId": service.session.session_id, "mode": "autopilot"},
        )
        active_mode = resp.get("mode", "autopilot")
    except Exception as e:
        logger.error(f"autopilot mode.set failed: {e}")
        await query.edit_message_text(
            "⚠️ Failed to set Autopilot mode — check bot logs for details."
        )
        return

    service.agent_mode = active_mode
    context.user_data["plan_mode"] = False

    if choice == "allow_all":
        service.allow_all_tools = True
    else:
        service.allow_all_tools = False
    service.save_prefs()

    buttons = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if m == active_mode else ''}{_MODE_LABELS[m]}",
                callback_data=f"mode:{m}",
            )
        ]
        for m in ("interactive", "plan", "autopilot")
    ]
    await query.edit_message_text(
        _mode_picker_header(active_mode)
        + "\n\n"
        + "\n".join(
            f"{_MODE_LABELS[m]}: {_MODE_DESCRIPTIONS[m]}" for m in _MODE_LABELS
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_streamer_reset_callback(query, context):
    """Reset session when user taps the 'Reset session now' button in /streamer_mode."""
    context.user_data["plan_mode"] = False
    if not await _safe_reset_session(query):
        return
    state = "ENABLED 📡" if service.streaming_enabled else "DISABLED 🔇"
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"✅ Session reset — Streamer Mode is now {state}.")


async def _handle_mcp_callback(query, context):
    """Handle mcp_toggle:<name> and mcp_reload callbacks."""
    import html as _html
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from src.core.mcp_config import (
        load_config,
        get_builtin_servers,
        toggle_server,
        MCP_CONFIG_PATH,
    )

    data = query.data

    if data == "mcp_reload":
        if not await _safe_reset_session(query):
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🔄 Session reloaded — MCP servers re-applied.")
        return

    # mcp_toggle:<name>
    parts = data.split(":", 1)
    if len(parts) < 2 or not parts[1]:
        await query.answer("Invalid toggle request.", show_alert=True)
        return
    name = parts[1]
    toggle_server(name)
    await query.answer()  # clear button spinner

    # Redraw the panel
    config = load_config()
    disabled = set(config.get("disabled", []))
    user_servers = config.get("mcpServers", {})
    builtins = get_builtin_servers()

    lines = ["🔌 <b>MCP Servers</b>\n", "<b>Built-in (SDK managed):</b>"]
    for bname, srv in builtins.items():
        lines.append(
            f"  ✅ {_html.escape(bname)} ({_html.escape(srv.get('type', '?'))})"
        )
    lines.append("\n<b>User-configured:</b>")
    for sname, srv in user_servers.items():
        icon = "⬜" if sname in disabled else "✅"
        kind = _html.escape(srv.get("type", "?"))
        detail = _html.escape(srv.get("url", srv.get("command", "")))
        lines.append(
            f"  {icon} <b>{_html.escape(sname)}</b> ({kind})  <code>{detail}</code>"
        )
    now_enabled = name not in disabled
    status = "enabled ✅" if now_enabled else "disabled ⬜"
    lines.append(
        f"\n<i>{_html.escape(name)} is now {status} — reload session to apply.</i>"
    )
    lines.append(f"\n<code>{_html.escape(str(MCP_CONFIG_PATH))}</code>")

    buttons = []
    for sname in user_servers:
        label = f"{'▶️ Enable' if sname in disabled else '⏸ Disable'} {sname}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"mcp_toggle:{sname}")]
        )
    buttons.append(
        [InlineKeyboardButton("🔄 Reload session now", callback_data="mcp_reload")]
    )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _handle_skill_callback(query, context) -> None:
    """Handle skill_toggle:<name> and skill_reload callbacks."""
    from src.core.skills_config import (
        scan_skills,
        get_disabled_skills,
        toggle_skill,
        skill_callback_name,
    )
    from src.core.context import ctx

    data = query.data

    if data == "skill_reload":
        if not await _safe_reset_session(query):
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🔄 Session reloaded — skills re-applied.")
        return

    # skill_toggle:<cb_name>  (cb_name is an MD5 digest from skill_callback_name())
    parts = data.split(":", 1)
    if len(parts) < 2 or not parts[1]:
        await query.answer("Invalid toggle request.", show_alert=True)
        return
    cb_name = parts[1]

    workspace = str(ctx.root_path) if ctx.root_path else None
    skills = scan_skills(workspace)

    # Match against truncated names (cb_name was produced by skill_callback_name())
    match = next((s for s in skills if skill_callback_name(s["name"]) == cb_name), None)
    if not match:
        await query.answer(
            "Skill not found — it may have been removed.", show_alert=True
        )
        return

    toggle_skill(match["name"])
    await query.answer()  # clear button spinner

    disabled = set(get_disabled_skills())
    text, markup = build_skills_panel(skills, disabled, workspace)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


def build_skills_panel(
    skills: list,
    disabled: set,
    workspace: "str | None",
) -> "tuple[str, InlineKeyboardMarkup]":
    """Build the skills status panel text + button markup. Public — used by commands.py."""
    import html as _html
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from src.core.skills_config import (
        get_skill_dirs,
        get_user_skill_dirs,
        USER_SKILLS_DIR,
        skill_callback_name,
    )

    dirs = get_skill_dirs(workspace)
    user_dirs = (
        get_user_skill_dirs()
    )  # already read by scan_skills; one more read here is fine

    lines = ["🧩 <b>Skills</b>\n", "<b>Skill directories:</b>"]
    if dirs:
        for d in dirs:
            source = "👤 user" if d in user_dirs else "📁 project"
            lines.append(f"  {source}  <code>{_html.escape(str(d))}</code>")
    else:
        lines.append(
            f"  None found  —  add files to <code>{_html.escape(str(USER_SKILLS_DIR))}</code>"
        )

    lines.append("\n<b>Loaded skills:</b>")
    if not skills:
        lines.append("  No skills found in scanned directories")
    else:
        for s in skills:
            icon = "⬜" if s["name"] in disabled else "✅"
            src_icon = "👤" if s["source"] == "user" else "📁"
            lines.append(f"  {icon} {src_icon} <b>{_html.escape(s['name'])}</b>")

    lines.append("\n<i>Reload session to apply changes.</i>")

    buttons = []
    for s in skills:
        cb_name = skill_callback_name(s["name"])
        display = s["name"] if len(s["name"]) <= 30 else s["name"][:29] + "…"
        label = f"{'▶️ Enable' if s['name'] in disabled else '⏸ Disable'} {display}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"skill_toggle:{cb_name}")]
        )
    buttons.append(
        [InlineKeyboardButton("🔄 Reload session now", callback_data="skill_reload")]
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


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

    if not await _safe_reset_session(query):
        return
    service.save_prefs()
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
    model_line = (
        f"\n🤖 Model: <code>{_html.escape(meta['model'])}</code>"
        if meta.get("model")
        else ""
    )
    await query.edit_message_text(
        f"{meta['icon']} <b>{_html.escape(meta['name'])}</b>{model_line}\n\n{_html.escape(meta['description'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        select_label, callback_data=f"agent_select:{key}"
                    ),
                    InlineKeyboardButton("◀️ Back", callback_data="agent_back"),
                ]
            ]
        ),
    )


async def _handle_agent_back_callback(query, context):
    """Return to the agent picker."""
    from src.core.agents import get_available_agents
    from src.handlers.commands import _build_agent_picker

    agents = get_available_agents()
    text, keyboard = _build_agent_picker(agents, service.selected_agent)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _handle_versions_callback(query, refresh: bool = False):
    """Handle versions_open / versions_refresh callbacks."""
    from src.handlers.commands import _build_versions_panel

    text, keyboard = await _build_versions_panel()
    if refresh:
        try:
            await query.edit_message_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    elif query.message:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await query.answer("Cannot open panel from this context", show_alert=True)


async def _handle_changelog_callback(query, component: str):
    """Fetch and display aggregated What's Changed for a component."""
    if component not in {"cli", "sdk"}:
        await query.answer("Unknown component", show_alert=True)
        return
    from src.handlers.commands import _fetch_whats_changed
    from src.config import TELEGRAM_MSG_LIMIT

    text = await _fetch_whats_changed(component)
    if not query.message:
        await query.answer("Cannot show changelog from this context", show_alert=True)
        return
    # Split on double-newline (release boundaries) to stay within Telegram limit
    if len(text) <= TELEGRAM_MSG_LIMIT:
        await query.message.reply_text(text, parse_mode="HTML")
        return
    chunks: list[str] = []
    current_chunk = ""
    limit = TELEGRAM_MSG_LIMIT - 30
    for block in text.split("\n\n"):
        candidate = f"{current_chunk}\n\n{block}" if current_chunk else block
        if len(candidate) > limit:
            if current_chunk:
                chunks.append(current_chunk)
            # If a single block exceeds limit, split it by lines
            if len(block) > limit:
                sub = ""
                for line in block.split("\n"):
                    sub_candidate = f"{sub}\n{line}" if sub else line
                    if len(sub_candidate) > limit:
                        if sub:
                            chunks.append(sub)
                        sub = line[: limit - 15] + "… (truncated)"
                    else:
                        sub = sub_candidate
                current_chunk = sub
            else:
                current_chunk = block
        else:
            current_chunk = candidate
    if current_chunk:
        chunks.append(current_chunk)
    for i, chunk in enumerate(chunks[:5]):
        suffix = (
            f"\n\n<i>({i + 1}/{min(len(chunks), 5)})</i>" if len(chunks) > 1 else ""
        )
        await query.message.reply_text(chunk + suffix, parse_mode="HTML")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    logger.info("🎯 button_handler ENTRY - CallbackQuery received")

    query = update.callback_query
    logger.info(f"🎯 Query data: {query.data}")

    data = query.data

    # For slow callbacks, show a loading toast instead of the generic empty answer
    if data in {"versions_open", "versions_refresh"} or data.startswith("changelog:"):
        try:
            await query.answer("⏳ Fetching version info…")
        except Exception as e:
            logger.error(f"❌ query.answer() failed: {e}", exc_info=True)
    else:
        try:
            await query.answer()
        except Exception as e:
            logger.error(f"❌ query.answer() failed: {e}", exc_info=True)

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
        elif data.startswith("skill_toggle:") or data == "skill_reload":
            await _handle_skill_callback(query, context)
        elif data.startswith("agent:"):
            await _handle_agent_callback(query, context)
        elif data.startswith("agent_detail:"):
            await _handle_agent_detail_callback(query, context)
        elif data.startswith("agent_select:"):
            await _apply_agent_selection(query, data.split(":", 1)[1])
        elif data == "agent_back":
            await _handle_agent_back_callback(query, context)
        elif data == "versions_open":
            await _handle_versions_callback(query, refresh=False)
        elif data == "versions_refresh":
            await _handle_versions_callback(query, refresh=True)
        elif data.startswith("changelog:"):
            await _handle_changelog_callback(query, data.split(":", 1)[1])
        elif data == "streamer:reset":
            await _handle_streamer_reset_callback(query, context)
        elif data.startswith("ls:"):
            await _handle_ls_callback(query, context)
        elif data == "sessions_all":
            await _handle_sessions_all_callback(query, context)
        elif data == "sessions_more" or data.startswith("sessions_more:"):
            page = int(data.split(":")[1]) if ":" in data else 0
            await _handle_sessions_more_callback(query, context, page)
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
            context.user_data["start_message_id"] = query.message.message_id
            context.user_data["start_chat_id"] = query.message.chat_id
            await query.message.reply_text("New project name:")
            return WAITING_PROJECT_NAME
    except Exception as e:
        logger.error(f"❌ Error handling button callback '{data}': {e}", exc_info=True)
    return ConversationHandler.END


async def create_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    name = re.sub(r"[^\w-]+", "_", update.message.text).strip("_")
    if not name:
        await update.message.reply_text("⚠️ Invalid name. Try again or /cancel.")
        return WAITING_PROJECT_NAME
    path = WORKSPACE_PATH / name
    already_exists = path.exists()
    if already_exists:
        await update.message.reply_text(
            f"⚠️ Project {name} already exists. Switched to it."
        )
    else:
        path.mkdir(exist_ok=True)
        await update.message.reply_text(f"✅ Created: {name}")
    try:
        await _switch_project(path, update.message, context)
        # Delete the project selector card (versions card persists separately)
        start_msg_id = context.user_data.pop("start_message_id", None)
        start_chat_id = context.user_data.pop("start_chat_id", None)
        if start_msg_id and start_chat_id:
            try:
                await context.bot.delete_message(
                    chat_id=start_chat_id,
                    message_id=start_msg_id,
                )
            except Exception as e:
                logger.warning(f"⚠️ Failed to delete start message: {e}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error setting directory: {e}")
    return ConversationHandler.END


async def cancel_create_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel project creation and re-show the start menu with project keyboard."""
    if not await security_check(update):
        return
    logger.info("Project creation cancelled, returning to start menu")
    # Clean up stored message IDs
    context.user_data.pop("start_message_id", None)
    context.user_data.pop("start_chat_id", None)
    from src.handlers.commands import build_start_menu

    selector_text, selector_kb = await build_start_menu()
    await update.message.reply_text(selector_text, reply_markup=selector_kb)
    return ConversationHandler.END


async def reject_command_during_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Reject slash commands (other than /cancel) during project name input."""
    if not await security_check(update):
        return
    await update.message.reply_text(
        "⚠️ Please enter a project name or use /cancel to go back."
    )
    return WAITING_PROJECT_NAME
