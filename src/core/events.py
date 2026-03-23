"""SDK event handler methods for CopilotService (mixin)."""

import asyncio
import logging
import time as _time_mod

from copilot.generated.session_events import SessionEventType

from src.core.context import ctx
from src.ui.formatters import format_tool_start, format_tool_complete, truncate_text
from src.core.context import streaming_mode as streaming_mode_var

logger = logging.getLogger(__name__)

_VISIBLE_TOOLS: frozenset[str] = frozenset(
    {
        "bash",
        "create",
        "edit",
        "task",
        "ask_user",
        "report_intent",
        "update_todo",
        "show_file",
    }
)


class EventHandlerMixin:
    """Mixin providing SDK event routing and per-type handler methods.

    Expects the host class to have:
      current_callback, _tool_call_names, _show_file_args, completion_callback,
      current_model, last_assistant_usage, last_session_usage
    """

    # ── Event router ──────────────────────────────────────────────────

    def _build_handler_map(self) -> dict:
        """Build the event-type → handler lookup once per instance."""
        return {
            SessionEventType.ASSISTANT_MESSAGE: self._on_assistant_message,
            SessionEventType.TOOL_EXECUTION_START: self._on_tool_start,
            SessionEventType.TOOL_EXECUTION_COMPLETE: self._on_tool_complete,
            SessionEventType.SUBAGENT_STARTED: self._on_subagent_started,
            SessionEventType.SUBAGENT_COMPLETED: self._on_subagent_completed,
            SessionEventType.SESSION_IDLE: self._on_session_idle,
            SessionEventType.SESSION_ERROR: self._on_session_error,
            SessionEventType.SESSION_USAGE_INFO: self._on_session_usage_info,
            SessionEventType.ASSISTANT_USAGE: self._on_assistant_usage,
            SessionEventType.SESSION_MODEL_CHANGE: self._on_session_model_change,
            SessionEventType.ASSISTANT_REASONING_DELTA: self._on_reasoning_delta,
            SessionEventType.ASSISTANT_MESSAGE_DELTA: self._on_assistant_message_delta,
            SessionEventType.SESSION_COMPACTION_START: self._on_compaction_start,
            SessionEventType.SESSION_COMPACTION_COMPLETE: self._on_compaction_complete,
            SessionEventType.SESSION_SNAPSHOT_REWIND: self._on_snapshot_rewind,
            SessionEventType.SYSTEM_NOTIFICATION: self._on_system_notification,
        }

    def _handle_event(self, event):
        """Route SDK events to per-type handler methods."""
        try:
            handler_map = self._handler_map_cache
        except AttributeError:
            handler_map = self._handler_map_cache = self._build_handler_map()
        handler = handler_map.get(event.type)
        if handler:
            handler(event)

    # ── Per-type handlers ─────────────────────────────────────────────

    def _on_assistant_message(self, event):
        """Capture the complete assistant message (streaming is disabled)."""
        content = getattr(event.data, "content", None)
        if content and self.current_callback:
            try:
                self._dispatch_async(self.current_callback, content)
            except Exception as e:
                logger.error(f"Failed to dispatch assistant message: {e}")

    def _on_tool_start(self, event):
        try:
            tool_name = (
                event.data.tool_name
                or getattr(event.data, "mcp_tool_name", None)
                or "unknown"
            )
            args = event.data.arguments
            tool_call_id = getattr(event.data, "tool_call_id", None)
            parent_tool_call_id = getattr(event.data, "parent_tool_call_id", None)

            if tool_call_id and tool_name != "unknown":
                self._tool_call_names[tool_call_id] = tool_name

            # Capture show_file args so we have path/diff at complete time
            if tool_name == "show_file" and tool_call_id and isinstance(args, dict):
                self._show_file_args[tool_call_id] = args

            # Log tool names and call IDs at INFO; full args only at DEBUG
            # to avoid leaking sensitive content (file_text, secrets) to log files.
            args_keys = list((args or {}).keys())
            logger.info(
                f"TOOL START: {tool_name} call_id={tool_call_id} parent={parent_tool_call_id} args_keys={args_keys}"
            )
            logger.debug(
                f"TOOL START args: {tool_name} call_id={tool_call_id} args={args}"
            )

            # Skip child tool events (those inside subagents) to match CLI
            # behavior — the CLI only shows agent lifecycle, not individual
            # view/grep/bash calls within each agent.
            if parent_tool_call_id:
                return

            # Only surface write-type tools + intent reporting; suppress
            # read-only exploration tools (grep, view, glob, sql, etc.)
            if tool_name not in _VISIBLE_TOOLS:
                return

            msg = format_tool_start(tool_name, args or {}).split("\n")[0]

            if ctx.status_callback:
                self._dispatch_async(ctx.status_callback, msg)
        except Exception as e:
            logger.error(f"Error handling TOOL_EXECUTION_START: {e}")

    def _on_tool_complete(self, event):
        try:
            tool_call_id = getattr(event.data, "tool_call_id", None)
            tool_name = getattr(event.data, "tool_name", None) or getattr(
                event.data, "mcp_tool_name", None
            )
            if not tool_name and tool_call_id:
                tool_name = self._tool_call_names.get(tool_call_id, "unknown")
            if not tool_name:
                tool_name = "unknown"

            parent_tool_call_id = getattr(event.data, "parent_tool_call_id", None)
            result = getattr(event.data, "result", None)
            result_content = (
                result.content if result and hasattr(result, "content") else None
            )

            logger.info(
                f"TOOL COMPLETE: {tool_name} call_id={tool_call_id} result_len={len(result_content) if result_content else 0}"
            )

            if tool_call_id and tool_call_id in self._tool_call_names:
                del self._tool_call_names[tool_call_id]

            # show_file: send file content as a formatted code block to the user
            if tool_name == "show_file" and result_content and self.current_callback:
                sf_args = (
                    self._show_file_args.pop(tool_call_id, {}) if tool_call_id else {}
                )
                path = sf_args.get("path", "")
                ext = path.rsplit(".", 1)[-1] if "." in path else ""
                caption = f"`{path}`\n" if path else ""
                # Telegram message limit ~4096 chars; reserve space for fences and caption
                max_content = 3900 - len(caption)
                body = result_content[:max_content] + (
                    "\n… (truncated)" if len(result_content) > max_content else ""
                )
                formatted = f"{caption}```{ext}\n{body}\n```"
                self._dispatch_async(self.current_callback, formatted)
                return

            if tool_name not in _VISIBLE_TOOLS:
                return

            max_len = None if streaming_mode_var.get() else 100
            msg = format_tool_complete(
                tool_name, result_content, max_result_length=max_len
            )
            if msg and ctx.status_callback:
                # Skip child tool completions (inside subagents) to match CLI.
                if parent_tool_call_id:
                    return
                self._dispatch_async(ctx.status_callback, msg)
        except Exception as e:
            logger.error(f"Error handling TOOL_EXECUTION_COMPLETE: {e}")

    def _on_subagent_started(self, event):
        # Increment before try so it always fires even if display_name lookup fails
        self._active_subagents += 1
        epoch = self._turn_epoch
        self._epoch_subagent_counts[epoch] = (
            self._epoch_subagent_counts.get(epoch, 0) + 1
        )
        try:
            display_name = getattr(event.data, "agent_display_name", None) or getattr(
                event.data, "agent_name", "Agent"
            )
            self._active_subagent_names.append(display_name)
            logger.info(
                f"SUBAGENT STARTED: {display_name} "
                f"(active={self._active_subagents}, epoch={epoch})"
            )
            msg = f"🤖 {display_name} started"
            cb = ctx.status_callback or ctx.notify_callback
            if cb:
                self._dispatch_async(cb, msg)
        except Exception as e:
            logger.error(f"Error handling SUBAGENT_STARTED: {e}")

    def _on_subagent_completed(self, event):
        current_epoch = self._turn_epoch
        if self._epoch_subagent_counts.get(current_epoch, 0) > 0:
            # Normal path: completion belongs to the current turn.
            self._epoch_subagent_counts[current_epoch] -= 1
            if self._epoch_subagent_counts[current_epoch] == 0:
                del self._epoch_subagent_counts[current_epoch]
        else:
            # Stale completion from a prior turn — decrement the global counter
            # for accuracy but skip deferred-finalization logic for this turn.
            stale_epochs = [
                k for k, v in self._epoch_subagent_counts.items() if v > 0
            ]
            if stale_epochs:
                stale = min(stale_epochs)
                self._epoch_subagent_counts[stale] -= 1
                if self._epoch_subagent_counts[stale] == 0:
                    del self._epoch_subagent_counts[stale]
                logger.debug(
                    "Ignoring stale SUBAGENT_COMPLETED from epoch %d "
                    "(current=%d)", stale, current_epoch,
                )
            else:
                logger.debug(
                    "Unexpected SUBAGENT_COMPLETED with no pending sub-agents "
                    "(epoch=%d)", current_epoch,
                )
            self._active_subagents = max(0, self._active_subagents - 1)
            return
        self._active_subagents = max(0, self._active_subagents - 1)
        try:
            display_name = getattr(event.data, "agent_display_name", None) or getattr(
                event.data, "agent_name", "Agent"
            )
            if display_name in self._active_subagent_names:
                self._active_subagent_names.remove(display_name)
            logger.info(f"SUBAGENT COMPLETED: {display_name} (active={self._active_subagents})")
            result = getattr(event.data, "result", None)
            result_content = (
                result.content if result and hasattr(result, "content") else None
            )
            if result_content:
                snippet = truncate_text(result_content, 120)
                msg = f"✓ {display_name} → {snippet}"
                # Store full result for post-finalization delivery.
                ctx.pending_subagent_results.append(
                    (display_name, result_content)
                )
            else:
                msg = f"✓ {display_name} completed"
            cb = ctx.status_callback or ctx.notify_callback
            if cb:
                self._dispatch_async(cb, msg)
        except Exception as e:
            logger.error(f"Error handling SUBAGENT_COMPLETED: {e}")
        # Always check deferred finalization — must be outside the try/except so
        # that a display/logging error cannot prevent completion_callback from firing.
        # Use the epoch dict: finalize only when the CURRENT turn has no more
        # pending sub-agents (prevents stale completions from triggering early).
        current_epoch_pending = self._epoch_subagent_counts.get(
            self._turn_epoch, 0
        )
        if current_epoch_pending == 0 and self._idle_deferred:
            self._idle_deferred = False
            self._dispatch_async(
                self._finalize_session_idle,
                self._deferred_status_cb,
                self._deferred_completion_cb,
            )

    def _notify_new_bg_agents(self) -> None:
        """Detect newly started background agents and notify via notify_callback."""
        bt = self._background_tasks_snapshot
        current_agents = getattr(bt, "agents", None) or []
        current_keys: set[tuple[str, str]] = {
            (
                getattr(a, "agent_type", "") or "",
                getattr(a, "description", "") or "",
            )
            for a in current_agents
        }
        current_ids: set[str] = {
            getattr(a, "agent_id", "") or ""
            for a in current_agents
            if getattr(a, "agent_id", None)
        }
        new_keys = current_keys - self._known_bg_agent_keys
        # Only update known-set when we have real data; a None/empty snapshot
        # would otherwise clear it and cause duplicate notifications next time.
        if current_agents:
            self._known_bg_agent_keys = current_keys
        if new_keys and ctx.notify_callback:
            lines = [
                f"• {k[0]}" + (f" — {k[1]}" if k[1] else "")
                for k in sorted(new_keys)
            ]
            count = len(new_keys)
            label = "agent" if count == 1 else "agents"
            msg = f"🤖 {count} background {label} started:\n" + "\n".join(lines)
            logger.info("Notifying user of new bg agents: %s", new_keys)
            self._dispatch_async(ctx.notify_callback, msg)

        # Update pending IDs and start/stop polling for completions.
        # Mirror the guard on _known_bg_agent_keys: only update when the
        # snapshot is non-empty. An empty snapshot after a [[BG_CHECK]]
        # probe (normal for a tool-only turn) must not clear the set and
        # cancel the poll prematurely.
        # Use in-place mutation to avoid aliasing issues with _cancel_bg_poll.
        if current_agents:
            completed = self._pending_bg_agent_ids - current_ids
            if completed:
                logger.info(
                    "BG poll: %d agent(s) left snapshot (completed)",
                    len(completed),
                )
            self._pending_bg_agent_ids.clear()
            self._pending_bg_agent_ids.update(current_ids)

        if self._pending_bg_agent_ids:
            self._start_bg_poll()
        else:
            self._cancel_bg_poll()

    def _on_session_idle(self, event):
        # Capture background_tasks from the SDK event payload
        self._background_tasks_snapshot = getattr(event.data, "background_tasks", None)
        self._notify_new_bg_agents()
        logger.info(
            f"⏸️ Session IDLE - Copilot finished (active_subagents={self._active_subagents})"
        )
        if self._active_subagents > 0:
            # Snapshot NOW — still inside send_and_wait, callbacks are live.
            # chat()'s finally block will clear them before the subagents finish,
            # so we capture here and trigger finalization from _on_subagent_completed.
            self._deferred_status_cb = ctx.status_callback
            self._deferred_completion_cb = self.completion_callback
            self._idle_deferred = True
            logger.info("⏸️ Deferring finalization — sub-agents still active")
            return
        # No sub-agents — snapshot and finalize immediately.
        status_cb = ctx.status_callback
        completion_cb = self.completion_callback
        self._dispatch_async(self._finalize_session_idle, status_cb, completion_cb)

    async def _finalize_session_idle(self, status_cb, completion_cb):
        """Await git info refresh, then fire status/completion callbacks."""
        t0 = _time_mod.monotonic()
        try:
            await self._refresh_git_info()
        except Exception as e:
            logger.error(f"[finalize] git_refresh failed: {e}", exc_info=True)
        t1 = _time_mod.monotonic()
        logger.debug(f"⏱️ [finalize] git_refresh={t1 - t0:.2f}s")
        if status_cb:
            try:
                if asyncio.iscoroutinefunction(status_cb):
                    await status_cb("")
                else:
                    status_cb("")
            except Exception as e:
                logger.error(f"[finalize] status_cb raised: {e}", exc_info=True)
        t2 = _time_mod.monotonic()
        logger.debug(f"⏱️ [finalize] status_callback={t2 - t1:.2f}s")
        if completion_cb:
            try:
                if asyncio.iscoroutinefunction(completion_cb):
                    await completion_cb()
                else:
                    completion_cb()
            except Exception as e:
                logger.error(f"[finalize] completion_cb raised: {e}", exc_info=True)
        t3 = _time_mod.monotonic()
        logger.info(
            f"⏱️ [finalize] total={t3 - t0:.2f}s"
        )

    def _on_session_error(self, event):
        error_msg = getattr(event.data, "message", None) or str(event.data)
        logger.error(f"❌ Session error event: {error_msg}")
        if ctx.status_callback:
            self._dispatch_async(ctx.status_callback, f"❌ Session error: {error_msg}")

    def _on_session_usage_info(self, event):
        self.last_session_usage = event.data
        logger.info(f"Session Usage Info Received: {event.data}")

    def _on_assistant_usage(self, event):
        self.last_assistant_usage = event.data
        if hasattr(event.data, "model") and event.data.model:
            self.current_model = event.data.model
            logger.info(f"Model from usage event: {self.current_model}")
        logger.info(f"Assistant Usage Received: {event.data}")

    def _on_session_model_change(self, event):
        new_model = getattr(event.data, "new_model", None)
        if new_model:
            logger.info(f"Session model changed to: {new_model}")
        else:
            logger.info("Session model change event received without new_model field")

    def _on_reasoning_delta(self, event):
        """Reasoning deltas are internal thinking — don't send to user, only log."""
        content = getattr(event.data, "delta_content", None) or getattr(
            event.data, "content", None
        )
        if content:
            logger.debug(f"🧠 Reasoning: {truncate_text(content, 200)}")

    def _on_assistant_message_delta(self, event):
        """Token-by-token streaming delta — forward to delta_callback when streaming enabled."""
        if not self.delta_callback:
            return
        content = getattr(event.data, "delta_content", None) or getattr(
            event.data, "content", None
        )
        if content:
            self._dispatch_async(self.delta_callback, content)

    def _on_compaction_start(self, event):
        logger.info("📦 Session compaction started")
        if ctx.status_callback:
            self._dispatch_async(
                ctx.status_callback, "📦 Context compaction in progress..."
            )

    def _on_snapshot_rewind(self, event):
        raw = getattr(event.data, "events_removed", None)
        try:
            events_removed = int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            events_removed = 0
        logger.info(f"↩️ Session snapshot rewind: {events_removed} events removed")
        if events_removed > 0:
            cb = ctx.status_callback or ctx.notify_callback
            if cb:
                self._dispatch_async(cb, f"↩️ {events_removed} event(s) rewound")

    def _on_system_notification(self, event):
        text = (getattr(event.data, "text", "") or "").strip()
        logger.info(f"🔔 System notification: {text[:200]}")
        cb = ctx.status_callback or ctx.notify_callback
        if text and cb:
            self._dispatch_async(cb, f"🔔 {text[:1000]}")

    def _on_compaction_complete(self, event):
        success = getattr(event.data, "success", None)
        status = "✅" if success else "⚠️"
        logger.info(f"📦 Session compaction complete (success={success})")
        if ctx.status_callback:
            self._dispatch_async(
                ctx.status_callback, f"{status} Context compaction complete"
            )

    # ── Async dispatch helper ─────────────────────────────────────────

    def _dispatch_async(self, callback, *args):
        """Dispatch an async or sync callback, keeping a strong task reference."""
        try:
            loop = asyncio.get_running_loop()
            if asyncio.iscoroutinefunction(callback):
                task = loop.create_task(callback(*args))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
            else:
                callback(*args)
        except Exception as e:
            logger.error(f"Failed to dispatch async callback: {e}", exc_info=True)
