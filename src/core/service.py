"""CopilotService — main orchestrator for the Copilot SDK integration.

Event handling lives in events.py (EventHandlerMixin).
Session lifecycle lives in session.py (SessionMixin).
"""

import os
import shutil
import asyncio
import re
import uuid
import logging
from pathlib import Path
from typing import Optional, List, Callable, Any, Dict

from copilot import CopilotClient

from src.config import (
    WORKSPACE_PATH,
    GITHUB_TOKEN,
    DEFAULT_MODEL,
)
from src.core.context import ctx
from src.core.git import get_git_info as _get_git_info
from src.core.filesystem import (
    get_directory_listing,
    get_project_structure,
    get_project_stats,
)
from src.core.usage import SessionUsageTracker, SessionInfo
from src.core.events import EventHandlerMixin
from src.core.session import SessionMixin
from src.core.prefs import apply_prefs, save_prefs as _save_prefs
from src.core.instructions import USER_INSTRUCTIONS_PATH, project_instructions_path

logger = logging.getLogger(__name__)


def _is_within(path: Path, root: Path) -> bool:
    """Check if path is within root directory (both must be resolved)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class _RequestWrapper:
    """Adapts SDK ask_user dict to an object with message/options/allowFreeform."""

    def __init__(self, req_dict: dict):
        self.message: str = req_dict.get("question", "")
        self.options: list = req_dict.get("choices", [])
        self.allowFreeform: bool = req_dict.get("allowFreeform", True)


class CopilotService(EventHandlerMixin, SessionMixin):
    """Singleton service wrapping the Copilot SDK client.

    Inherits:
      EventHandlerMixin  — SDK event routing (_handle_event, _on_* methods)
      SessionMixin       — lifecycle (start, stop, reset_session, change_model, etc.)
    """

    def __init__(self):
        # Initialize context root
        ctx.set_root(WORKSPACE_PATH)
        self._system_cli_path: Optional[str] = None
        self._bundled_cli_path: Optional[str] = None
        self._active_cli_path: Optional[str] = None
        self._active_cli_source: str = "unknown"
        self._cli_fallback_reason: Optional[str] = None
        self._pending_runtime_warning: Optional[str] = None
        self._prefer_cli_default_model: bool = False
        self.client = self._create_client(ctx.root_path)

        self.session = None  # type: ignore[assignment]
        self.session_id: str = str(uuid.uuid4())[:8]
        self._event_unsubscribe: Optional[Callable] = None
        self._usage_unsubscribe: Optional[Callable] = None
        self.current_callback: Optional[Callable] = None
        self.delta_callback: Optional[Callable] = None
        self.interaction_callback: Optional[Callable] = None
        self.completion_callback: Optional[Callable] = None
        self.last_assistant_usage: Any = None
        self.last_session_usage: Any = None
        self.current_model: Optional[str] = DEFAULT_MODEL
        self.user_selected_model: Optional[str] = None
        self.current_reasoning_effort: Optional[str] = None
        self._models_cache: List[Dict[str, Any]] = []
        self._context_limits_cache: Dict[str, int] = {}
        self._is_running: bool = False
        self.project_selected: bool = False
        self.project_name: str = ""
        self._tool_call_names: Dict[str, str] = {}
        self._show_file_args: Dict[str, dict] = {}
        self.session_expired: bool = False
        self.session_end_callback: Optional[Callable[[str], Any]] = None
        self.allow_all_tools: bool = False
        self._session_approved_tools: set[str] = set()
        self.infinite_sessions_enabled: bool = False
        self.streaming_enabled: bool = False
        self.agent_mode: str = "interactive"  # interactive | plan | autopilot
        self.selected_agent: Optional[str] = (
            None  # key of active custom agent, or None for default
        )
        self.extra_dirs: List[str] = []  # additional directories added via /add_dir

        # Session info from SDK events (single source of truth)
        self.session_info = SessionInfo()

        self._chat_lock = asyncio.Lock()
        self._cancelled = False  # Set by /cancel to signal abort to chat_handler

        # Usage tracking (accumulates from SDK events)
        self.usage_tracker = SessionUsageTracker()

        # Restore persisted user preferences (model, effort, agent, mode, …)
        apply_prefs(self)

    def save_prefs(self) -> None:
        """Persist current user preferences to disk."""
        _save_prefs(self)

    # ── Working directory ─────────────────────────────────────────────

    async def set_working_directory(self, path: str) -> str:
        """Switch the Copilot client to a new working directory.

        Restarts the client process so the SDK picks up the new CWD.
        """
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")

        # Defense-in-depth: ensure the resolved path is within an allowed root.
        from src.config import GRANTED_PROJECT_PATHS

        allowed_roots = [WORKSPACE_PATH.resolve()] + [
            gp.resolve() for gp in GRANTED_PROJECT_PATHS
        ]
        if not any(_is_within(p, root) for root in allowed_roots):
            raise PermissionError(f"Path is outside allowed workspace boundaries: {p}")

        current_root = ctx.root_path
        logger.info(f"📂 Requested CWD change: {current_root} -> {p}")

        if str(p) != str(current_root) or self.session_expired or not self.session:
            # Full client restart: CWD changed, session died, or session missing
            if str(p) != str(current_root):
                reason = "CWD change"
            elif self.session_expired:
                reason = "session recovery"
            else:
                reason = "missing session"
            logger.info(f"🔄 Full client restart ({reason}): {current_root} -> {p}")

            # Wait for any active chat to finish
            async with self._chat_lock:
                pass

            if self._is_running:
                logger.info("Stopping old Copilot Client...")
                await self.stop()

            ctx.set_root(p)
            self.session_info = SessionInfo()
            self._models_cache = []

            self.client = self._create_client(p)
            logger.info(f"🔄 CopilotClient re-initialized with CWD: {p}")

            logger.info("Starting Copilot Client with new CWD...")
            await self.start()
            await asyncio.sleep(0.2)
            logger.info("✅ Copilot Client restarted.")

        self.project_selected = True
        self.project_name = p.name
        self.extra_dirs = []  # dirs are project-scoped; clear on project switch
        logger.info(f"Workspace change complete: {current_root} -> {ctx.root_path}")
        return str(ctx.root_path)

    def _build_client_config(
        self, cwd: Path, cli_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build CopilotClient config for a given cwd and optional CLI path."""
        config: Dict[str, Any] = {"cwd": str(cwd)}
        if GITHUB_TOKEN:
            config["github_token"] = GITHUB_TOKEN
        if cli_path:
            config["cli_path"] = cli_path
        return config

    def _discover_system_cli_path(self) -> Optional[str]:
        """Return the installed Copilot CLI path if available."""
        candidates = (
            shutil.which("copilot"),
            os.path.expanduser("~/.local/bin/copilot"),
        )
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        return None

    def _discover_bundled_cli_path(self) -> Optional[str]:
        """Return the SDK-bundled Copilot CLI path if available."""
        try:
            from copilot.client import _get_bundled_cli_path

            bundled_cli = _get_bundled_cli_path()
        except Exception as e:
            logger.debug(f"Could not resolve bundled CLI path: {e}")
            return None

        if bundled_cli and os.path.isfile(bundled_cli):
            return bundled_cli
        return None

    def _create_client(self, cwd: Path) -> CopilotClient:
        """Create a client preferring the installed CLI before bundled fallback."""
        self._system_cli_path = self._discover_system_cli_path()
        self._bundled_cli_path = self._discover_bundled_cli_path()
        self._cli_fallback_reason = None

        if self._system_cli_path:
            self._active_cli_path = self._system_cli_path
            self._active_cli_source = "system"
            logger.info(f"🔧 Preferring system Copilot CLI: {self._system_cli_path}")
            return CopilotClient(
                self._build_client_config(cwd, cli_path=self._system_cli_path)
            )

        self._active_cli_path = self._bundled_cli_path
        self._active_cli_source = "bundled" if self._bundled_cli_path else "unknown"
        if self._bundled_cli_path:
            logger.info(f"📦 Using bundled Copilot CLI: {self._bundled_cli_path}")
        return CopilotClient(self._build_client_config(cwd))

    def should_fallback_to_bundled_cli(self, error: Exception) -> bool:
        """Return True when the system CLI should be replaced with bundled CLI."""
        return (
            isinstance(error, RuntimeError)
            and "protocol version mismatch" in str(error).lower()
            and self._active_cli_source == "system"
            and bool(self._bundled_cli_path)
        )

    def activate_bundled_cli_fallback(self, reason: str) -> bool:
        """Switch the active client to the bundled CLI."""
        if not self._bundled_cli_path:
            self._bundled_cli_path = self._discover_bundled_cli_path()
        if not self._bundled_cli_path:
            return False

        self._active_cli_path = self._bundled_cli_path
        self._active_cli_source = "bundled"
        self._cli_fallback_reason = reason
        self.client = CopilotClient(
            self._build_client_config(ctx.root_path, cli_path=self._bundled_cli_path)
        )
        logger.info(f"📦 Falling back to bundled Copilot CLI: {self._bundled_cli_path}")
        return True

    def get_working_directory(self) -> str:
        return str(ctx.root_path)

    # ── Session info helpers ──────────────────────────────────────────

    def get_session_info(self) -> SessionInfo:
        """Return session context information from SDK events."""
        return self.session_info

    def get_temp_dir(self) -> Path:
        """Returns path to the session's temp dir, creating it if needed."""
        p = ctx.root_path / f".tmp-{self.session_id}"
        if not p.exists():
            p.mkdir(exist_ok=True)
        return p

    def cleanup_temp_dir(self):
        p = ctx.root_path / f".tmp-{self.session_id}"
        if p.exists():
            try:
                shutil.rmtree(p)
                logger.info(f"Cleaned up temp dir: {p}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp dir: {e}")

    # ── Usage / metadata ──────────────────────────────────────────────

    def get_usage_metadata(self) -> tuple[str, str, str]:
        """Returns (project, model, cost) tuple for footer construction."""
        try:
            if self.session_info.cwd:
                project = Path(self.session_info.cwd).name
            else:
                project = self.project_name or Path(ctx.root_path).name

            model = "Auto"
            cost = "0.0"

            if self.last_assistant_usage:
                if (
                    hasattr(self.last_assistant_usage, "model")
                    and self.last_assistant_usage.model
                ):
                    model = self.last_assistant_usage.model
                elif self.current_model:
                    model = self.current_model
                if (
                    hasattr(self.last_assistant_usage, "cost")
                    and self.last_assistant_usage.cost is not None
                ):
                    cost = f"{self.last_assistant_usage.cost:.2f}"
            elif self.current_model:
                model = self.current_model

            return project, model, cost
        except Exception as e:
            logger.error(f"get_usage_metadata failed: {e}")
            return "Unknown", "Auto", "0.0"

    async def get_usage_report(self) -> str:
        """Returns formatted usage stats from the accumulated SessionUsageTracker."""
        return await self.usage_tracker.get_usage_summary()

    async def _get_cli_version_for_path(self, cli_path: Optional[str]) -> str:
        """Get a CLI version from a specific executable path."""
        if not cli_path or not os.path.isfile(cli_path):
            return "not found"

        try:
            proc = await asyncio.create_subprocess_exec(
                cli_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            match = re.search(r"(\d+\.\d+\.\d+)", stdout.decode())
            if match:
                return match.group(1)
        except Exception as e:
            logger.debug(f"CLI --version failed ({cli_path}): {e}")

        return "unknown"

    def _get_bundled_cli_version_for_path(self, cli_path: Optional[str]) -> str:
        """Read the SDK-bundled CLI version from its adjacent VERSION file."""
        if not cli_path:
            return "unknown"
        version_file = Path(cli_path).with_name("VERSION")
        if not version_file.is_file():
            return "unknown"
        try:
            version = version_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.debug(f"Could not read bundled CLI VERSION file: {e}")
            return "unknown"
        return version or "unknown"

    async def _get_active_cli_version(
        self, cli_path: Optional[str], active_source: str
    ) -> str:
        """Get the version of the CLI actually in use."""
        if self._is_running and cli_path and cli_path == self._active_cli_path:
            try:
                status = await self.client.get_status()
                if hasattr(status, "version") and status.version:
                    return str(status.version)
            except Exception as e:
                logger.debug(f"SDK get_status() failed: {e}")

        if active_source == "bundled":
            bundled_version = self._get_bundled_cli_version_for_path(cli_path)
            if bundled_version != "unknown":
                return bundled_version

        return await self._get_cli_version_for_path(cli_path)

    async def _probe_cli_compatibility(
        self, cli_path: str, cwd: Optional[Path] = None
    ) -> tuple[bool, Optional[str]]:
        """Check whether a CLI binary can complete the SDK startup handshake."""
        probe_client = CopilotClient(
            self._build_client_config(cwd or ctx.root_path, cli_path=cli_path)
        )
        started = False
        try:
            await probe_client.start()
            started = True
            return True, None
        except RuntimeError as e:
            if "protocol version mismatch" in str(e).lower():
                return False, str(e)
            raise
        finally:
            try:
                if started:
                    await probe_client.stop()
            except Exception as e:
                logger.debug(f"CLI compatibility probe cleanup failed: {e}")

    async def get_cli_runtime_info(
        self, probe: bool = False
    ) -> Dict[str, Optional[str]]:
        """Return installed and active CLI metadata for runtime diagnostics."""
        installed_path = self._system_cli_path or self._discover_system_cli_path()
        bundled_path = self._bundled_cli_path or self._discover_bundled_cli_path()
        active_path = self._active_cli_path
        active_source = self._active_cli_source
        fallback_reason = self._cli_fallback_reason

        if probe and not self._is_running and installed_path and bundled_path:
            is_compatible, probe_reason = await self._probe_cli_compatibility(
                installed_path
            )
            if is_compatible:
                active_path = installed_path
                active_source = "system"
                fallback_reason = None
            else:
                active_path = bundled_path
                active_source = "bundled"
                fallback_reason = probe_reason
            self._active_cli_path = active_path
            self._active_cli_source = active_source
            self._cli_fallback_reason = fallback_reason

        installed_version = await self._get_cli_version_for_path(installed_path)
        active_version = await self._get_active_cli_version(active_path, active_source)

        return {
            "installed_path": installed_path,
            "installed_version": installed_version,
            "active_path": active_path,
            "active_version": active_version,
            "active_source": active_source,
            "fallback_reason": fallback_reason,
        }

    def pop_pending_runtime_warning(self) -> Optional[str]:
        """Return and clear the next runtime warning intended for the user."""
        warning = self._pending_runtime_warning
        self._pending_runtime_warning = None
        return warning

    def get_display_model(self) -> str:
        """Return the best user-facing model label for cockpit/status views."""
        if self.user_selected_model:
            return self.user_selected_model
        if self.current_model:
            return self.current_model
        if self._prefer_cli_default_model:
            return "CLI default"
        return "Auto"

    async def set_agent_mode(self, mode: str) -> bool:
        """Set desired agent mode. self.agent_mode is always updated (optimistic local cache)
        so the mode is re-applied on the next session start even if no session is active now.
        Returns True only if the RPC call to the live session also succeeded."""
        _VALID_MODES = ("interactive", "plan", "autopilot")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"Invalid agent mode '{mode}'. Must be one of: {_VALID_MODES}"
            )
        self.agent_mode = (
            mode  # intentional: store desired mode regardless of session state
        )
        if self.session and self.session.session_id:
            try:
                await self.client._client.request(
                    "session.mode.set",
                    {"sessionId": self.session.session_id, "mode": mode},
                )
                logger.info(f"Agent mode set to '{mode}' via RPC")
                return True
            except Exception as e:
                logger.warning(
                    f"session.mode.set RPC failed: {e} — will apply on next session start"
                )
        return False

    # ── Session export ────────────────────────────────────────────────

    async def export_session_to_file(self) -> Optional[str]:
        """Exports the current session history to a markdown file using SDK get_messages()."""
        if not self.session:
            logger.warning("No active session to export")
            return None

        try:
            from src.ui.session_exporter import format_session_markdown

            logger.info("📥 Retrieving session history...")
            events = await self.session.get_messages()

            if not events:
                logger.warning("Session has no events to export")
                return None

            logger.info(f"📊 Retrieved {len(events)} events")

            metadata = {
                "session_id": self.session_id,
                "start_time": ctx.session_start_time,
                "project_name": self.project_name or ctx.root_path.name,
                "current_model": self.current_model,
            }

            logger.info("📝 Formatting session markdown...")
            markdown_content = format_session_markdown(events, metadata)

            filename = f"copilot-telegram-bot-{self.session_id}.md"
            filepath = ctx.root_path / filename

            filepath.write_text(markdown_content, encoding="utf-8")
            logger.info(f"✅ Session exported to: {filepath}")

            return str(filepath)

        except Exception as e:
            logger.error(f"❌ Session export failed: {e}", exc_info=True)
            return None

    # ── CLI / auth helpers ────────────────────────────────────────────

    async def get_cli_version(self) -> str:
        """Get the version of the CLI currently selected for runtime use."""
        runtime = await self.get_cli_runtime_info()
        return runtime["active_version"] or "unknown"

    async def get_auth_status(self) -> str:
        if not self._is_running:
            await self.start()
        try:
            status = await self.client.get_auth_status()
            logger.debug(f"Auth Check: {status}")
            return status.login if hasattr(status, "login") else "User"
        except Exception:
            return "User"

    async def get_git_info(self) -> str:
        """Get git info — delegates to core.git module."""
        return await _get_git_info(self.session_info.branch, self.session_info.cwd)

    # ── Models ────────────────────────────────────────────────────────

    async def get_available_models(self) -> List[Dict[str, str]]:
        if not self._is_running:
            await self.start()
        try:
            models = await self.client.list_models()
            results = []
            for m in models:
                mid = str(m.id) if hasattr(m, "id") else str(m)
                mult = "1x"
                if hasattr(m, "billing") and hasattr(m.billing, "multiplier"):
                    multiplier_val = m.billing.multiplier
                    if isinstance(multiplier_val, (int, float)):
                        if multiplier_val == int(multiplier_val):
                            mult = f"{int(multiplier_val)}x"
                        else:
                            mult = f"{multiplier_val}x"
                    else:
                        mult = f"{multiplier_val}x"

                # Cache context window limit from SDK capabilities
                if hasattr(m, "capabilities") and hasattr(m.capabilities, "limits"):
                    ctx_tokens = getattr(
                        m.capabilities.limits, "max_context_window_tokens", None
                    )
                    if ctx_tokens:
                        self._context_limits_cache[mid] = int(ctx_tokens)

                supports_reasoning = bool(
                    hasattr(m, "supported_reasoning_efforts")
                    and m.supported_reasoning_efforts
                )
                supported_efforts = getattr(m, "supported_reasoning_efforts", []) or []
                default_effort = getattr(m, "default_reasoning_effort", None)

                results.append(
                    {
                        "id": mid,
                        "multiplier": mult,
                        "supports_reasoning": supports_reasoning,
                        "supported_efforts": supported_efforts,
                        "default_effort": default_effort,
                    }
                )
            self._models_cache = results

            # If the user has an active session, ensure its model always appears
            # in the picker, even if models.list hasn't been updated yet.
            active_model = self.current_model
            if active_model and not any(r["id"] == active_model for r in results):
                results.insert(
                    0,
                    {
                        "id": active_model,
                        "multiplier": "1x",
                        "supports_reasoning": False,
                        "supported_efforts": [],
                        "default_effort": None,
                    },
                )
                logger.info(
                    f"ℹ️  Injected active session model into list: {active_model}"
                )

            logger.info(
                f"📊 Cached context limits for {len(self._context_limits_cache)} models"
            )
            return results
        except Exception as e:
            logger.error(f"Failed to fetch models: {e}")
            return []

    def get_model_context_limit(self, model_name: str) -> int:
        """Return context window size for a model from cached SDK data."""
        DEFAULT_CONTEXT_LIMIT = 128_000
        if not model_name:
            return DEFAULT_CONTEXT_LIMIT
        # Exact match first
        if model_name in self._context_limits_cache:
            return self._context_limits_cache[model_name]
        # Substring match (e.g., "claude" matches "claude-sonnet-4")
        name_lower = model_name.lower()
        for key, limit in self._context_limits_cache.items():
            if name_lower in key.lower() or key.lower() in name_lower:
                return limit
        return DEFAULT_CONTEXT_LIMIT

    # ── Project info ──────────────────────────────────────────────────

    async def get_project_info_header(
        self, context_user_data: Optional[dict] = None
    ) -> str:
        """Build rich project info header with model, mode, path, branch, and structure."""
        model = self.get_display_model()
        mode = (
            "Plan"
            if (context_user_data and context_user_data.get("plan_mode"))
            else "Chat"
        )
        path_str = str(ctx.root_path).replace(os.path.expanduser("~"), "~")
        git_info = await self.get_git_info()
        branch_line = f"🔀 Branch: {git_info[1:]}\n" if git_info else ""
        tree = self.get_project_structure()

        header = (
            f"🤖 Model: {model}\n"
            f"⚙️ Mode: {mode}\n"
            f"📂 Path: {path_str}\n"
            f"{branch_line}"
            f"📂 Structure:\n{tree}"
        )
        return header

    async def get_cockpit_message(
        self, context_user_data: Optional[dict] = None
    ) -> str:
        """Build the cockpit message shown after project selection."""
        from src.ui.menus import get_cockpit_content
        from src.core.mcp_config import get_enabled_servers, load_config
        from src.core.skills_config import scan_skills, get_disabled_skills

        model = self.get_display_model()
        # Use persisted agent_mode as source of truth; sync context flag for footer display
        mode_map = {"plan": "Plan", "autopilot": "Autopilot"}
        mode = mode_map.get(self.agent_mode, "Chat")
        if context_user_data is not None:
            context_user_data["plan_mode"] = self.agent_mode == "plan"
        path_str = str(ctx.root_path).replace(os.path.expanduser("~"), "~")
        git_info = await self.get_git_info()
        branch = git_info[1:] if git_info else ""
        file_count, folder_count = get_project_stats(self.session_info.cwd)
        effort = self.current_reasoning_effort or ""
        all_servers = load_config()["mcpServers"]
        mcp_enabled = len(get_enabled_servers())
        mcp_total = len(all_servers)
        # Skills count
        all_skills = scan_skills(self.session_info.cwd)
        disabled_skills = set(get_disabled_skills())
        skills_enabled = sum(1 for s in all_skills if s["name"] not in disabled_skills)
        skills_total = len(all_skills)
        # Get selected agent display name and user agent count via SDK RPC
        from src.core.agents import get_available_agents, get_builtin_agent_keys

        agent_name = ""
        agent_user_count = 0
        agent_builtin_count = 0
        if self.session:
            try:
                result = await self.session.rpc.agent.list()
                agent_user_count = len(result.agents)
                if self.selected_agent:
                    meta = next(
                        (a for a in result.agents if a.name == self.selected_agent),
                        None,
                    )
                    agent_name = (
                        meta.display_name or meta.name if meta else self.selected_agent
                    )
            except Exception:
                pass
        if self.client:
            builtin_keys = await get_builtin_agent_keys(self.client)
            user_agent_keys = {a["key"] for a in get_available_agents()}
            agent_builtin_count = sum(
                1 for k in builtin_keys if k not in user_agent_keys
            )
        if not agent_name and self.selected_agent:
            # Fallback to filesystem if session not available
            agents = get_available_agents()
            meta = next((a for a in agents if a["key"] == self.selected_agent), None)
            agent_name = meta["name"] if meta else self.selected_agent
            agent_user_count = agent_user_count or len(agents)
        return get_cockpit_content(
            project_name=self.project_name or Path(self.session_info.cwd).name,
            model=model,
            mode=mode,
            path=path_str,
            branch=branch,
            file_count=file_count,
            folder_count=folder_count,
            effort=effort,
            mcp_enabled=mcp_enabled,
            mcp_total=mcp_total,
            agent_name=agent_name,
            agent_builtin_count=agent_builtin_count,
            agent_user_count=agent_user_count,
            streaming=self.streaming_enabled,
            allow_all_tools=self.allow_all_tools,
            extra_dirs=self.extra_dirs or None,
            skills_enabled=skills_enabled,
            skills_total=skills_total,
            instructions_user=USER_INSTRUCTIONS_PATH.exists(),
            instructions_project=_proj_instr.exists()
            if (_proj_instr := project_instructions_path(self.session_info.cwd))
            else False,
            session_summary=await self._get_session_summary(),
        )

    def get_directory_listing(self) -> str:
        """Returns flat list of current directory content."""
        return get_directory_listing(self.session_info.cwd)

    async def _get_session_summary(self) -> str:
        """Return the current session's title/summary from workspace.yaml.

        Reads the persisted summary only — never triggers LLM generation.
        Title generation is handled in the background by schedule_title_generation.
        """
        from src.core.titler import read_session_summary
        from src.ui.menus import _clean_summary

        sid = self.session_info.session_id
        if not sid:
            return ""
        if self.session_info.name:
            return _clean_summary(self.session_info.name)
        return _clean_summary(read_session_summary(sid)) or ""

    def get_project_structure(self, max_depth: int = 2) -> str:
        """Returns nested project structure with file sizes."""
        return get_project_structure(self.session_info.cwd, max_depth)

    # ── Chat ──────────────────────────────────────────────────────────

    async def chat(
        self,
        user_message: str,
        content_callback: Optional[Callable[[str], Any]] = None,
        status_callback: Optional[Callable[[str], Any]] = None,
        interaction_callback: Optional[Callable[[str, Any], Any]] = None,
        completion_callback: Optional[Callable[[], Any]] = None,
        delta_callback: Optional[Callable[[str], Any]] = None,
        attachments: Optional[list] = None,
    ):
        """Send a message to the Copilot session and wait for completion.

        Callbacks:
          content_callback(chunk) — accumulates response text chunks.
          status_callback(status) — tool events trigger permanent messages.
          interaction_callback(kind, payload) — for permission/input dialogs.
          completion_callback() — fires when the model finishes (SESSION_IDLE).
          delta_callback(chunk) — fires for each streaming token delta (streaming mode only).

        Args:
          attachments — optional list of SDK attachment dicts.
        """
        async with self._chat_lock:
            self._cancelled = False
            if not self.session:
                await self.start()
            self.current_callback = content_callback
            self.delta_callback = delta_callback
            ctx.status_callback = status_callback
            self.interaction_callback = interaction_callback
            self.completion_callback = completion_callback

            try:
                msg_options: dict = {"prompt": user_message}
                if attachments:
                    msg_options["attachments"] = attachments
                await self.session.send_and_wait(msg_options)
                # abort() causes send_and_wait to return normally once session.idle fires
                if self._cancelled:
                    raise asyncio.CancelledError("Request cancelled by user")
            finally:
                self.current_callback = None
                self.delta_callback = None
                ctx.status_callback = None
                self.interaction_callback = None
                self.completion_callback = None


# Global Singleton
service = CopilotService()
