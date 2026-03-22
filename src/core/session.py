"""Session lifecycle methods for CopilotService (mixin)."""

import asyncio
import contextlib
import json
import os
import re
import tempfile
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from src.config import (
    DEFAULT_MODEL,
    INTERACTION_TIMEOUT,
    PERMISSION_TIMEOUT,
)
from src.core.context import ctx
from src.core.mcp_config import get_enabled_servers
from src.core.skills_config import get_skill_dirs_for_session, get_disabled_skills
from src.core.agents import (
    get_available_agents,
    get_builtin_agent_keys,
    parse_agent_prompt,
    AGENTS_DIR,
)
from src.core.usage import SessionUsageTracker, SessionInfo
from src.core.instructions import (
    USER_INSTRUCTIONS_PATH,
    project_instructions_path,
    safe_read_instructions,
    EMPTY_FILE_SENTINEL,
)
from copilot.types import PermissionRequestResult
from copilot.generated.session_events import PermissionRequestKind

logger = logging.getLogger(__name__)


def _is_reasoning_unsupported_error(error: Exception) -> bool:
    """Return True when the CLI rejects reasoning_effort for the selected model."""
    message = str(error).lower()
    return (
        "does not support reasoning effort" in message
        or "does not support reasoning_effort" in message
    )


# ── Tool allowlist (auto-approved without asking user) ────────────────

_TOOL_ALLOWLIST = frozenset(
    {
        "report_intent",
        "task",
        "view",
        "glob",
        "grep",
        "fetch_copilot_cli_documentation",
        "ask_user",
        "update_todo",
    }
)


class _PermissionRequest:
    """Lightweight container for tool permission request data."""

    __slots__ = ("tool_name", "arguments", "can_offer_session_approval")

    def __init__(
        self,
        name: str,
        args: dict,
        can_offer_session_approval: bool = False,
    ):
        self.tool_name = name
        self.arguments = args
        self.can_offer_session_approval = can_offer_session_approval


_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _patch_session_attachments(session_id: str) -> None:
    """Fix legacy session files where attachments field is null instead of [].

    The CLI binary serializes missing attachments as null, but newer SDK
    versions require an array. Patch atomically before resuming so resume
    doesn't fail with a -32603 corruption error.
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        logger.warning("Skipping patch — non-UUID session_id: %r", session_id)
        return

    base = (Path.home() / ".copilot" / "session-state").resolve()
    session_dir = (base / session_id).resolve()
    if not str(session_dir).startswith(str(base) + "/"):
        logger.warning("Skipping patch — path escape for session_id: %r", session_id)
        return

    events_file = session_dir / "events.jsonl"
    if not events_file.exists():
        return

    try:
        original = events_file.read_text(encoding="utf-8")
        lines = []
        changed = False
        for line in original.splitlines(keepends=True):
            try:
                obj = json.loads(line)
                if (
                    isinstance(obj, dict)
                    and obj.get("data") is not None
                    and isinstance(obj["data"], dict)
                    and obj["data"].get("attachments") is None
                    and "attachments" in obj["data"]
                ):
                    obj["data"]["attachments"] = []
                    line = json.dumps(obj, ensure_ascii=False) + "\n"
                    changed = True
            except json.JSONDecodeError:
                pass
            lines.append(line)

        if not changed:
            return

        patched = "".join(lines)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=session_dir, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(patched)
            os.replace(tmp_path, events_file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        logger.info(
            "Patched attachments:null -> [] in session %s events.jsonl", session_id
        )
    except Exception as exc:
        logger.warning("Could not patch %s — resuming anyway: %s", events_file, exc)


def _apply_agent_config(svc, cfg: dict) -> None:
    """Inject config_dir and custom_agents into a session config dict.

    Always registers all installed agents so the CLI binary includes them in
    the task tool's agent_type enum (required since SDK v0.1.28 — configDir
    alone no longer triggers auto-discovery into the tool schema).

    When the user has explicitly selected an agent it is registered with
    infer=False so the runtime uses it without override; all other agents are
    registered with infer=True so the model can invoke them on demand.
    """
    cfg["config_dir"] = str(AGENTS_DIR.parent)  # ~/.copilot

    available = get_available_agents()
    if not available:
        return

    if svc.selected_agent:
        prompt = parse_agent_prompt(svc.selected_agent)
        if not prompt:
            logger.warning(
                f"Agent '{svc.selected_agent}' file not found"
                " — falling back to default agent"
            )
            svc.selected_agent = None

    wire_agents = []
    for a in available:
        prompt = parse_agent_prompt(a["key"])
        if not prompt:
            continue
        is_selected = a["key"] == svc.selected_agent
        wire_agents.append(
            {
                "name": a["key"],
                "display_name": a.get("name", a["key"]),
                "description": a.get("description", ""),
                "prompt": prompt,
                # infer=False for the explicitly selected agent so the runtime
                # activates it directly; infer=True for all others so they are
                # available in the task tool schema but not auto-activated.
                "infer": not is_selected,
            }
        )

    if wire_agents:
        cfg["custom_agents"] = wire_agents
        selected_info = svc.selected_agent or "none selected"
        logger.info(
            f"Custom agents registered: {len(wire_agents)} (selected: {selected_info})"
        )


def _build_agents_context() -> str:
    """Return a system-message snippet listing all available custom agents.

    Returns "" if no agents are installed so nothing is appended.
    """
    agents = get_available_agents()
    if not agents:
        return ""
    lines = ["Available custom agents (user installs via /agent command):"]
    for a in agents:
        desc = f" — {a['description']}" if a["description"] else ""
        lines.append(f"- {a['key']}: {a['name']}{desc}")
    return "\n".join(lines)


def _load_instructions(cwd: str | None) -> str:
    """Read and combine user-level and project-level copilot-instructions.md files.

    Returns a string ready to append to the system message, or "" if neither
    file exists.  The CLI headless server does not auto-load these in SDK mode,
    so we inject them explicitly here.
    """
    proj_path = project_instructions_path(cwd)
    candidates = [
        ("User instructions", USER_INSTRUCTIONS_PATH, Path.home()),
        ("Project instructions", proj_path, proj_path.parent if proj_path else None),
    ]
    parts: list[str] = []
    for label, path, allowed_root in candidates:
        content = safe_read_instructions(path, allowed_root)
        if content and content != EMPTY_FILE_SENTINEL:
            parts.append(f"--- {label} ---\n{content}")
    return "\n\n".join(parts)


class SessionMixin:
    """Mixin providing session lifecycle methods for CopilotService.

    Expects the host class to have:
      client, session, session_id, session_info, _is_running,
      _event_unsubscribe, _usage_unsubscribe, current_model,
      user_selected_model, current_reasoning_effort, interaction_callback,
      session_expired, session_end_callback, usage_tracker,
      _tool_call_names, _chat_lock, last_session_usage, last_assistant_usage,
      _handle_event (from EventHandlerMixin), cleanup_temp_dir
    """

    # ── Public lifecycle ──────────────────────────────────────────────

    async def start(self):
        """Start the Copilot client and create an initial session."""
        if not self._is_running:
            logger.info("Starting Copilot Client...")
            try:
                await self.client.start()
            except RuntimeError as e:
                if not self.should_fallback_to_bundled_cli(e):
                    logger.error(f"Failed to start client: {e}")
                    raise

                logger.warning(
                    "System Copilot CLI failed protocol handshake; "
                    "retrying with bundled CLI."
                )
                try:
                    await self.client.stop()
                except Exception as stop_error:
                    logger.debug(
                        f"Ignoring stop error after failed client start: {stop_error}"
                    )

                if not self.activate_bundled_cli_fallback(str(e)):
                    logger.error(f"Failed to start client: {e}")
                    raise
                await self.client.start()
            except Exception as e:
                logger.error(f"Failed to start client: {e}")
                raise
            self._is_running = True
            logger.info("Copilot Client Started.")
            # Prime the built-in agent key cache once so concurrent cockpit
            # requests never race to populate it.
            try:
                await get_builtin_agent_keys(self.client)
            except Exception as e:
                logger.warning(f"Could not prime builtin agent cache: {e}")
        if not self.session:
            await self._create_session()

    async def stop(self):
        """Stop the Copilot client and clean up resources."""
        logger.info("Stopping Copilot Client...")
        self.cleanup_temp_dir()
        self._unsubscribe_handlers()

        if self.session:
            try:
                await self.session.destroy()
            except Exception as e:
                logger.warning(f"Error destroying session during stop: {e}")
            self.session = None

        if self._is_running:
            try:
                await asyncio.wait_for(self.client.stop(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("⏱️ Graceful stop timed out, forcing stop...")
                await self.client.force_stop()
            except ExceptionGroup as eg:
                for err in eg.exceptions:
                    logger.warning(f"⚠️ Client stop error: {err}")
            except Exception as e:
                logger.error(f"Error during client stop: {e}")
                try:
                    await self.client.force_stop()
                except Exception:
                    pass
            self._is_running = False

        logger.info("Copilot Client Stopped.")

    async def reset_session(self, model: Optional[str] = None):
        """Destroy the current session and create a fresh one."""
        if model:
            self._prefer_cli_default_model = False
            self.current_model = model
            self.user_selected_model = model
        logger.info("Resetting session...")

        self.cleanup_temp_dir()
        self.session_id = str(uuid.uuid4())[:8]
        self._tool_call_names.clear()
        self._show_file_args.clear()
        self._session_approved_tools.clear()
        self.last_session_usage = None
        self.last_assistant_usage = None

        # Reset session info so /session shows fresh data
        self.session_info = SessionInfo()

        self._unsubscribe_handlers()

        if self.session:
            try:
                await self.session.destroy()
            except Exception as e:
                logger.warning(f"Error destroying session: {e}")
            self.session = None

        await self._create_session()

    async def change_model(self, model: str, reasoning_effort: str = None):
        """Switch model, preserving conversation history when possible.

        Uses session.set_model() for model-only switches (no reasoning_effort
        change) so history is kept. When reasoning_effort changes, a session
        reset is required because the SDK applies it only at session creation.
        Falls back to reset_session() if set_model() fails or no session exists.
        """
        effort_changed = reasoning_effort != self.current_reasoning_effort
        self._prefer_cli_default_model = False
        self.current_reasoning_effort = reasoning_effort
        self.current_model = model
        self.user_selected_model = model
        self.save_prefs()

        if self.session and not effort_changed:
            try:
                logger.info(f"🔄 Switching model to {model} (preserving history)")
                await self.session.set_model(model)
                return
            except Exception as e:
                logger.warning(f"set_model() failed ({e}), falling back to reset")

        logger.info(f"🔄 Switching model to {model} (session reset)")
        await self.reset_session(model)

    async def populate_session_metadata(self):
        """Fetch session metadata (name, created, modified) from client.list_sessions()."""
        if not self.session_info.session_id:
            logger.warning("No session_id available to fetch metadata")
            return

        try:
            sessions = await self.client.list_sessions()
            meta = next(
                (
                    s
                    for s in sessions
                    if getattr(s, "sessionId", None) == self.session_info.session_id
                ),
                None,
            )
            if meta:
                self.session_info.name = getattr(meta, "summary", None)
                self.session_info.created = getattr(meta, "startTime", None)
                self.session_info.modified = getattr(meta, "modifiedTime", None)
                logger.info(
                    f"📊 Session metadata fetched - Name: {self.session_info.name}, Created: {self.session_info.created}"
                )
            else:
                logger.warning(
                    f"Session {self.session_info.session_id} not found in list_sessions()"
                )
        except Exception as e:
            logger.warning(f"Failed to fetch session metadata: {e}")

    async def resume_session_by_id(self, session_id: str):
        """Resume an existing Copilot session by ID."""
        logger.info(f"Resuming session: {session_id}")
        self.cleanup_temp_dir()
        self.session_id = str(uuid.uuid4())[:8]
        self._tool_call_names.clear()
        self._show_file_args.clear()
        self.last_session_usage = None
        self.last_assistant_usage = None
        self.session_info = SessionInfo()
        self._unsubscribe_handlers()

        if self.session:
            try:
                await self.session.destroy()
            except Exception as e:
                logger.warning(f"Error destroying old session: {e}")
            self.session = None

        model = self.user_selected_model or self.current_model or DEFAULT_MODEL
        resume_config = {
            "model": model,
            "streaming": self.streaming_enabled,
            "on_permission_request": self._on_permission_request,
            "hooks": {
                "on_pre_tool_use": self._permission_bridge,
                "on_session_end": self._on_session_end,
            },
            "on_user_input_request": self._user_input_bridge,
        }
        if self.current_reasoning_effort:
            resume_config["reasoning_effort"] = self.current_reasoning_effort

        mcp_servers = get_enabled_servers()
        if mcp_servers:
            resume_config["mcp_servers"] = mcp_servers
            logger.info(f"MCP servers loaded: {list(mcp_servers.keys())}")

        skill_dirs = get_skill_dirs_for_session(str(ctx.root_path))
        if skill_dirs:
            resume_config["skill_directories"] = skill_dirs
        disabled = get_disabled_skills()
        if disabled:
            resume_config["disabled_skills"] = disabled

        _apply_agent_config(self, resume_config)

        _patch_session_attachments(session_id)
        self.session = await self.client.resume_session(session_id, **resume_config)
        self.current_model = model
        logger.info(f"✅ Session resumed: {session_id}")

        await self._apply_stored_agent_mode("session resume")

        self.session_info.workspace_path = str(ctx.root_path)
        self._extract_session_start_context()
        self._event_unsubscribe = self.session.on(self._handle_event)
        self.session_expired = False
        ctx.clear_tracked_files()
        ctx.session_start_time = datetime.now()
        self.usage_tracker = SessionUsageTracker()
        self.usage_tracker.session_start_time = time.time()
        if self.current_model:
            self.usage_tracker.selected_model = self.current_model
        self._usage_unsubscribe = self.session.on(self.usage_tracker.handle_event)

    # ── Session hooks ─────────────────────────────────────────────────

    async def _apply_stored_agent_mode(self, context: str = "session"):
        """Apply self.agent_mode via RPC if it differs from the default 'interactive'.
        Skips the RPC for 'interactive' because that is the SDK's default on session start;
        sending it would be redundant. If this assumption ever changes, remove the guard."""
        if self.agent_mode == "interactive":
            return
        try:
            await self.client._client.request(
                "session.mode.set",
                {"sessionId": self.session.session_id, "mode": self.agent_mode},
            )
            logger.info(f"Agent mode restored to '{self.agent_mode}' on {context}")
        except Exception as e:
            logger.warning(f"Failed to restore agent mode on {context}: {e}")

    async def _on_session_end(self, input_data, invocation):
        """Hook called by SDK when session ends (timeout, error, etc.)."""
        reason = input_data.get("reason", "unknown")
        error = input_data.get("error")
        logger.info(f"📛 Session ended | reason={reason} error={error}")

        self.cleanup_temp_dir()

        if reason in ("timeout", "error"):
            self.session_expired = True
            self.session_info.status = "Expired"
            if self.session_end_callback:
                try:
                    msg = f"⚠️ Session expired ({reason}). Use /start to begin a new session."
                    if error:
                        msg += f"\nError: {error}"
                    await self.session_end_callback(msg)
                except Exception as e:
                    logger.error(f"❌ Failed to send session end notification: {e}")
        return None

    # ── Internal helpers ──────────────────────────────────────────────

    def _unsubscribe_handlers(self):
        """Unsubscribe from event and usage handlers (deduplicated helper)."""
        if self._event_unsubscribe:
            try:
                self._event_unsubscribe()
            except Exception as e:
                logger.warning(f"Failed to unsubscribe event handler: {e}")
            self._event_unsubscribe = None

        if self._usage_unsubscribe:
            try:
                self._usage_unsubscribe()
            except Exception as e:
                logger.warning(f"Failed to unsubscribe usage tracker: {e}")
            self._usage_unsubscribe = None

    async def _create_session(self):
        """Create and configure a new Copilot SDK session."""
        selected_model = self.user_selected_model or self.current_model
        model = (
            None
            if self._prefer_cli_default_model and not selected_model
            else selected_model or DEFAULT_MODEL
        )
        if model:
            logger.info(f"Creating new session with model: {model}")
        else:
            logger.info("Creating new session with CLI default model")

        session_config = {
            "streaming": self.streaming_enabled,
            "on_permission_request": self._on_permission_request,
            "hooks": {
                "on_pre_tool_use": self._permission_bridge,
                "on_session_end": self._on_session_end,
            },
            "on_user_input_request": self._user_input_bridge,
            "system_message": {
                "mode": "append",
                "content": (
                    "You are assisting via a Telegram bot. "
                    "Respond concisely and always use Plain text. "
                    "Avoid HTML tags. Keep responses focused and actionable. "
                    "**Format:** Response must be **PLAIN TEXT** (no markdown code blocks, use simple bullets)."
                ),
            },
        }
        if model:
            session_config["model"] = model
        if self.extra_dirs:
            # Sanitize: strip newlines/control chars from paths before injecting into system prompt
            safe_dirs = [
                d.replace("\n", " ").replace("\r", " ").replace("\x00", "")
                for d in self.extra_dirs
            ]
            extra = "\n".join(f"- {d}" for d in safe_dirs)
            session_config["system_message"]["content"] += (
                f"\n\nYou also have access to these additional directories:\n{extra}"
            )

        instructions = _load_instructions(self.session_info.cwd)
        if instructions:
            session_config["system_message"]["content"] += f"\n\n{instructions}"
            logger.info("Copilot instructions injected into system message")

        agents_ctx = _build_agents_context()
        if agents_ctx:
            session_config["system_message"]["content"] += f"\n\n{agents_ctx}"
            logger.info("Custom agents list injected into system message")
        if self.current_reasoning_effort:
            session_config["reasoning_effort"] = self.current_reasoning_effort
        if self.infinite_sessions_enabled:
            session_config["infinite_sessions"] = {"enabled": True}

        mcp_servers = get_enabled_servers()
        if mcp_servers:
            session_config["mcp_servers"] = mcp_servers
            logger.info(f"MCP servers loaded: {list(mcp_servers.keys())}")

        skill_dirs = get_skill_dirs_for_session(str(ctx.root_path))
        if skill_dirs:
            session_config["skill_directories"] = skill_dirs
            logger.info(f"Skill directories: {skill_dirs}")
        disabled = get_disabled_skills()
        if disabled:
            session_config["disabled_skills"] = disabled
            logger.info(f"Disabled skills: {disabled}")

        _apply_agent_config(self, session_config)
        try:
            self.session = await self.client.create_session(**session_config)
        except Exception as e:
            if not (
                "reasoning_effort" in session_config
                and _is_reasoning_unsupported_error(e)
            ):
                raise

            import html

            active_cli = html.escape(getattr(self, "_active_cli_source", "unknown"))
            rejected_model = html.escape(str(model))
            logger.warning(
                "Session creation rejected model/reasoning combination "
                f"model={model!r} effort={self.current_reasoning_effort!r}; "
                "retrying with the CLI default model and no reasoning effort."
            )
            self.current_reasoning_effort = None
            self.user_selected_model = None
            self.current_model = None
            self._prefer_cli_default_model = True
            self.save_prefs()
            retry_config = dict(session_config)
            retry_config.pop("reasoning_effort", None)
            retry_config.pop("model", None)
            self.session = await self.client.create_session(**retry_config)
            model = None
            self._pending_runtime_warning = (
                "⚠️ <b>Model fallback applied</b>\n\n"
                f"Model <code>{rejected_model}</code> with reasoning effort is not "
                f"supported by the active {active_cli} CLI. "
                "The bot started a session with the active CLI default model instead."
            )

        self.current_model = model
        if model:
            logger.info(f"✅ Session created with model: {model}")
        else:
            logger.info("✅ Session created with CLI default model")

        await self._apply_stored_agent_mode("session start")

        # Populate initial session info with workspace details
        self.session_info.workspace_path = str(ctx.root_path)
        self._extract_session_start_context()

        # Subscribe to SDK events
        self._event_unsubscribe = self.session.on(self._handle_event)
        self.session_expired = False
        ctx.clear_tracked_files()
        ctx.session_start_time = datetime.now()

        # Reset usage tracker for new session BEFORE subscribing
        self.usage_tracker = SessionUsageTracker()
        self.usage_tracker.session_start_time = time.time()
        if self.current_model:
            self.usage_tracker.selected_model = self.current_model
        self._usage_unsubscribe = self.session.on(self.usage_tracker.handle_event)

    def _extract_session_start_context(self):
        """Capture session context from session.start event via get_messages()."""

        # Note: SESSION_START event doesn't fire reliably, so we query messages.
        # This is called synchronously after session creation — we schedule the
        # async work as a task.
        async def _extract():
            try:
                messages = await self.session.get_messages()
                if messages and len(messages) > 0:
                    first_event = messages[0]
                    if first_event.type.value == "session.start":
                        self.session_info.session_id = getattr(
                            first_event.data, "session_id", None
                        )
                        self.session_info.selected_model = getattr(
                            first_event.data, "selected_model", None
                        )
                        self.session_info.copilot_version = getattr(
                            first_event.data, "copilot_version", None
                        )
                        self.session_info.producer = getattr(
                            first_event.data, "producer", None
                        )

                        if hasattr(first_event.data, "context"):
                            context = first_event.data.context
                            if context and not isinstance(context, str):
                                self.session_info.cwd = getattr(context, "cwd", None)
                                self.session_info.branch = getattr(
                                    context, "branch", None
                                )
                                self.session_info.git_root = getattr(
                                    context, "git_root", None
                                )
                                self.session_info.repository = getattr(
                                    context, "repository", None
                                )
                                logger.info(
                                    f"📍 Session context captured from session.start - "
                                    f"CWD: {self.session_info.cwd}, Branch: {self.session_info.branch}, "
                                    f"Git Root: {self.session_info.git_root}"
                                )

                        if (
                            self.session_info.selected_model
                            and not self.user_selected_model
                        ):
                            self.current_model = self.session_info.selected_model
                            logger.info(f"🤖 SDK selected model: {self.current_model}")
            except Exception as e:
                logger.warning(f"Could not retrieve session context from messages: {e}")

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_extract())
        except RuntimeError:
            logger.debug("No running event loop — skipping session context extraction")

    async def _on_permission_request(self, request, invocation=None):
        """Gate built-in CLI tool execution via Telegram Allow/Deny UI."""
        kind = request.kind
        logger.debug(
            f"🔐 _on_permission_request CALLED | kind={kind.value} |"
            f" tool={getattr(request, 'tool_name', None)} |"
            f" file={getattr(request, 'file_name', None) or getattr(request, 'path', None)} |"
            f" interaction_callback={self.interaction_callback is not None}"
        )

        if self.allow_all_tools:
            logger.info(f"✅ Auto-approved (allow_all mode): {kind.value}")
            return PermissionRequestResult(kind="approved")

        # Safe read-only / non-destructive operations — always auto-approve.
        if kind in (
            PermissionRequestKind.READ,
            PermissionRequestKind.URL,
            PermissionRequestKind.MEMORY,
        ):
            logger.info(f"✅ Auto-approved safe kind: {kind.value}")
            return PermissionRequestResult(kind="approved")

        # SHELL, WRITE, and MCP all require user approval below.
        if kind == PermissionRequestKind.MCP:
            display_name = f"mcp:{request.server_name}/{request.tool_name}"
            display_args: dict = request.args or {}
        else:
            display_name = request.tool_name or kind.value
            display_args = request.args or {}

        _denied = PermissionRequestResult(
            kind="denied-no-approval-rule-and-could-not-request-from-user"
        )

        # Check session-wide approval granted earlier this session.
        # Only the specific display_name is stored (not the generic kind.value),
        # so the lookup is an exact match against the approved tool name.
        if display_name in self._session_approved_tools:
            logger.info(f"✅ Auto-approved (session rule): {display_name}")
            return PermissionRequestResult(kind="approved")

        if not self.interaction_callback:
            logger.warning(
                f"🔴 No interaction_callback — denying {kind.value}: {display_name}"
            )
            return _denied

        try:
            logger.info(
                f"🔔 Requesting user permission for {kind.value}: {display_name}"
            )
            # Always offer session approval for destructive kinds (SHELL, WRITE, MCP).
            # The CLI rarely sets canOfferSessionApproval for these kinds.
            can_session = kind in (
                PermissionRequestKind.SHELL,
                PermissionRequestKind.WRITE,
                PermissionRequestKind.MCP,
            ) or bool(getattr(request, "can_offer_session_approval", False))
            perm_req = _PermissionRequest(
                display_name,
                display_args,
                can_offer_session_approval=can_session,
            )

            result = await asyncio.wait_for(
                self.interaction_callback("permission", perm_req),
                timeout=PERMISSION_TIMEOUT,
            )

            if result == "allow_session":
                # Store only the specific tool display_name so that
                # approving "bash" does not silently approve "sh", "zsh",
                # or any other tool that shares the same generic kind value.
                self._session_approved_tools.add(display_name)
                logger.info(f"✅ User approved for session: {display_name}")
                return PermissionRequestResult(kind="approved")

            if result == "allow":
                logger.info(f"✅ User approved {kind.value}: {display_name}")
                return PermissionRequestResult(kind="approved")

            logger.info(f"❌ User denied {kind.value}: {display_name}")
            return PermissionRequestResult(kind="denied-interactively-by-user")

        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning(
                f"⏱️ Permission timeout or cancellation, denying"
                f" {kind.value}: {display_name}"
            )
            return _denied
        except Exception as e:
            logger.error(f"❌ Permission request failed: {e}", exc_info=True)
            return _denied

    async def _permission_bridge(self, input_data, invocation):
        """Bridge between SDK on_pre_tool_use and Telegram permission UI."""
        tool_name = input_data.get("toolName", "unknown")

        # Auto-approve when allow_all_tools is enabled
        if self.allow_all_tools:
            logger.info(f"✅ Auto-approved (allow_all mode): {tool_name}")
            return {"permissionDecision": "allow"}

        # Auto-approve tools in allowlist
        if tool_name in _TOOL_ALLOWLIST:
            logger.info(f"✅ Auto-approved allowlisted tool: {tool_name}")
            return {"permissionDecision": "allow"}

        # Check session-wide approval.
        if tool_name in self._session_approved_tools:
            logger.info(f"✅ Auto-approved (session rule): {tool_name}")
            return {"permissionDecision": "allow"}

        # For MCP tools, also check the composite "mcp:{server}/{tool}"
        # key that _on_permission_request stores on "allow_session".
        server_name = input_data.get("serverName", "")
        if server_name:
            mcp_key = f"mcp:{server_name}/{tool_name}"
            if mcp_key in self._session_approved_tools:
                logger.info(f"✅ Auto-approved (session rule, MCP): {mcp_key}")
                return {"permissionDecision": "allow"}

        # For non-allowlisted tools, defer to the permission system
        # (PERMISSION_REQUESTED event → _on_permission_request → user prompt).
        # Returning "ask" tells the CLI to fire PERMISSION_REQUESTED normally.
        # This avoids a double-prompt race where both hooks fire concurrently
        # for the same bash/create call.
        #
        # ARCHITECTURAL NOTE (github-copilot-sdk==0.1.32):
        # The "ask" permissionDecision value was introduced in SDK ≥0.1.28.
        # If the SDK is downgraded below that version this return value will
        # silently fall-through to allow, creating a fail-open security hole.
        # Always pin github-copilot-sdk in pyproject.toml to a version that
        # supports "ask" (currently pinned at ==0.1.32).
        logger.info(f"🔔 Deferring to permission system for tool: {tool_name}")
        return {"permissionDecision": "ask"}

    async def _refresh_git_info(self):
        """Re-query git branch/status and update session_info (3s timeout)."""
        try:
            cwd = self.session_info.cwd or str(ctx.root_path)
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    "git rev-parse --abbrev-ref HEAD",
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=3.0,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            branch = stdout.decode().strip()
            if branch:
                if branch != self.session_info.branch:
                    logger.info(
                        f"🔀 Git branch updated: {self.session_info.branch} → {branch}"
                    )
                self.session_info.branch = branch
        except asyncio.TimeoutError:
            logger.warning("⏱️ Git info refresh timed out (3s)")
        except Exception as e:
            logger.debug(f"Git info refresh failed: {e}")

    async def _user_input_bridge(self, request, invocation=None):
        """Bridge between SDK's ask_user format and Telegram interaction_callback."""
        question = request.get("question", "")
        choices = request.get("choices", [])
        allow_freeform = request.get("allowFreeform", True)

        logger.info(
            f"🔔 user_input_bridge called | Question: '{question[:60]}...' | "
            f"Choices: {choices} | Callback exists: {self.interaction_callback is not None}"
        )

        try:
            if not self.interaction_callback:
                logger.error("❌ No interaction_callback registered!")
                return {"answer": "", "wasFreeform": False}

            from src.core.service import _RequestWrapper

            wrapped = _RequestWrapper(request)

            logger.info(
                f"⏳ Calling interaction_callback with {INTERACTION_TIMEOUT}s timeout..."
            )
            result = await asyncio.wait_for(
                self.interaction_callback("input", wrapped),
                timeout=INTERACTION_TIMEOUT,
            )
            logger.info(f"✅ interaction_callback returned: {result}")

            was_cancel = result == "cancel" or not result
            was_freeform = allow_freeform and (not choices or result not in choices)

            response = {
                "answer": result if not was_cancel else "",
                "wasFreeform": was_freeform,
            }
            logger.info(f"📤 Returning to SDK: {response}")
            return response

        except asyncio.TimeoutError:
            logger.error(f"⏱️ user_input_bridge timed out after {INTERACTION_TIMEOUT}s")
            return {"answer": "", "wasFreeform": False}
        except Exception as e:
            logger.error(f"❌ user_input_bridge failed: {e}", exc_info=True)
            return {"answer": "", "wasFreeform": False}
