import uuid
import asyncio
import time
import logging
from pathlib import Path
from typing import Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.config import INTERACTION_TIMEOUT
from src.core.service import service
from src.core.context import streaming_mode
from src.ui.streamer import MessageSender
from src.ui.menus import _write_session_summary

from src.handlers.utils import security_check, check_project_selected

logger = logging.getLogger(__name__)

_PLAN_PROMPT = (
    "You are in PLAN MODE. Create a clear, actionable plan — do NOT write code.\n\n"
    "1. Research the necessary information.\n"
    "2. Ask clarifying questions if needed.\n"
    "3. Deliver a structured, phased plan with short bullets, file references, and rationale.\n\n"
    "RULES: "
    "Do NOT create any files (e.g., `plan.md`) in the session workspace. The user cannot access the session file system.\n\n"
    "Do NOT write any code. Your entire response should be the plan in clear text"
    "No code blocks. Keep it scannable and mobile-friendly.\n\n"
    "FORMAT: Response must be PLAIN TEXT (no markdown code blocks, use simple bullets)."
    "---\n\n"
)

_GENERAL_PROMPT = (
    "You are in GENERAL Mode.\n"
    "ROLE: You are an AI-powered coding assistant on a Telegram bot, built on the GitHub Copilot CLI.\n"
    "JOB: Help the user build, debug, and understand code using natural language, deeply integrated with the GitHub workflow.\n\n"
    "RULES: No code blocks. Keep it scannable and mobile-friendly.\n\n"
    "FORMAT: Response must be PLAIN TEXT (no markdown code blocks, use simple bullets)."
    "---\n"
)

# Pending Interactions (Future map) - Shared with callbacks
# Structure: {interaction_id: {"future": Future, "timestamp": float, "chat_id": int, "context": ContextTypes.DEFAULT_TYPE}}
PENDING_INTERACTIONS: dict[str, dict[str, Any]] = {}
INTERACTION_TTL = INTERACTION_TIMEOUT  # matches send_and_wait timeout


def cleanup_pending_interactions():
    """Removes interactions that are older than INTERACTION_TTL."""
    now = time.time()
    to_remove = []
    for interaction_id, data in list(PENDING_INTERACTIONS.items()):
        if isinstance(data, dict):
            future = data.get("future")
            timestamp = data.get("timestamp", now)
            # Remove if done/cancelled or expired
            if future and (future.done() or (now - timestamp) > INTERACTION_TTL):
                to_remove.append(interaction_id)
                if not future.done():
                    logger.warning(
                        f"Interaction {interaction_id} expired after {INTERACTION_TTL}s"
                    )
                    try:
                        future.set_exception(TimeoutError("User interaction timed out"))
                    except Exception:
                        pass
        elif hasattr(data, "done") and data.done():
            # Legacy format - just a future
            to_remove.append(interaction_id)

    for k in to_remove:
        PENDING_INTERACTIONS.pop(k, None)

    if to_remove:
        logger.info(f"Cleaned up {len(to_remove)} pending interactions")


async def chat_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE, override_text: str = None
):
    if not await security_check(update):
        return
    if not await check_project_selected(update):
        return

    if service.session_expired:
        await update.message.reply_text(
            "⚠️ Session expired. Use /start to begin a new session."
        )
        return

    # Prevent sending while session is busy (feature #5)
    if service._chat_lock.locked():
        await update.message.reply_text(
            "⏳ Please wait for the current request to finish. Use /cancel to abort it."
        )
        return

    user_text = override_text or (update.message.text if update.message else "") or ""

    attachments = None  # SDK-native attachments list
    attachment = update.message.document or (
        update.message.photo[-1] if update.message.photo else None
    )
    if attachment:
        try:
            file_obj = await attachment.get_file()
            original_name = getattr(attachment, "file_name", None)
            if not original_name:
                ext = ".jpg" if update.message.photo else ""
                original_name = f"file_{int(time.time())}{ext}"
            # Security: strip directory components to prevent path traversal
            # (e.g. file_name="../../.env" → ".env" → safe fallback)
            original_name = Path(original_name).name.replace("\x00", "")
            if not original_name:
                original_name = f"upload_{int(time.time())}"
            temp_dir = service.get_temp_dir()
            download_path = temp_dir / original_name
            await file_obj.download_to_drive(custom_path=download_path)
            # Use SDK-native attachments (feature #7)
            attachments = [{"type": "file", "path": str(download_path)}]
            user_text = (update.message.caption or "Describe this file").strip()
            await update.message.reply_text(f"📎 Uploaded: {original_name}")
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            await update.message.reply_text(f"⚠️ Upload failed: {e}")
            return

    if not user_text:
        return

    original_user_text = user_text
    if context.user_data.get("plan_mode"):
        user_text = _PLAN_PROMPT + user_text
    else:
        user_text = _GENERAL_PROMPT + user_text

    # Initialize message sender with the user's message chat
    sender = MessageSender(update.message)
    await sender.create_working()  # Show "Working..." immediately at the top
    completion_event = asyncio.Event()
    response_chunks: list[str] = []
    tool_event_count = 0  # Track tool events

    # ---- Callbacks wired into service.chat() ----

    async def tool_log(status: str):
        """Handle tool status events."""
        nonlocal tool_event_count
        if not status:  # Empty status = clear signal, ignore
            return
        logger.debug(
            f"🔍 tool_log received (streaming={service.streaming_enabled}): {repr(status[:80])}"
        )
        if service.streaming_enabled:
            await sender.update_working(status)  # Edit Working... card in-place
        else:
            await sender.send_tool_event(status)  # Separate permanent card
        tool_event_count += 1

    async def stream_content(text_chunk: str):
        """Accumulate response chunks (no streaming to Telegram)."""
        response_chunks.append(text_chunk)

    async def stream_delta_callback(chunk: str):
        """Forward streaming delta to MessageSender for live editing."""
        await sender.stream_delta(chunk)

    async def on_completion():
        """Signal that the model has finished."""
        completion_event.set()

    async def interaction_callback(kind: str, payload: Any) -> Any:
        cleanup_pending_interactions()
        interaction_id = str(uuid.uuid4())[:8]
        future = asyncio.get_running_loop().create_future()
        chat_id = update.effective_chat.id if update and update.effective_chat else None

        # Store future with metadata
        PENDING_INTERACTIONS[interaction_id] = {
            "future": future,
            "timestamp": time.time(),
            "chat_id": chat_id,
            "context": context,
            "kind": kind,
            "options": getattr(payload, "options", []) if kind == "input" else None,
        }

        logger.info(
            f"⚡ Interaction created: {interaction_id} | Kind: {kind} | Chat: {chat_id}"
        )

        try:
            if kind == "permission":
                tool_name = getattr(payload, "tool_name", "unknown")
                args = getattr(payload, "arguments", {})

                # Plain text permission request (no markdown)
                msg_text = f"🛡️ Permission: {tool_name}"

                # Add args preview if present
                if args and len(str(args)) > 0:
                    args_preview = str(args)[:80]
                    suffix = "..." if len(str(args)) > 80 else ""
                    msg_text += f"\nArguments: {args_preview}{suffix}"

                msg_text += "\n\nAllow?"
                # Store tool_name in interaction_data for later reference
                PENDING_INTERACTIONS[interaction_id]["tool_name"] = tool_name
                buttons = [
                    [
                        InlineKeyboardButton(
                            "✅ Allow", callback_data=f"perm:{interaction_id}:allow"
                        ),
                        InlineKeyboardButton(
                            "❌ Deny", callback_data=f"perm:{interaction_id}:deny"
                        ),
                    ]
                ]
                await sender.pause_stream()
                await _send_interaction_msg(update, context, chat_id, msg_text, buttons)

            elif kind == "input":
                prompt = getattr(payload, "message", str(payload))
                options = getattr(payload, "options", [])

                # Plain text prompt (no markdown)
                msg_text = f"❓ Copilot Asks:\n{prompt}\n\nSelect an option:"
                buttons = []
                for i, opt in enumerate(options):
                    label = str(opt)
                    btn_label = (label[:30] + "..") if len(label) > 30 else label
                    callback_data = f"input:{interaction_id}:{label}"
                    if len(callback_data.encode("utf-8")) > 64:
                        callback_data = f"input:{interaction_id}:{i}"
                    buttons.append(
                        [InlineKeyboardButton(btn_label, callback_data=callback_data)]
                    )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            "❌ Cancel", callback_data=f"input:{interaction_id}:cancel"
                        )
                    ]
                )
                await sender.pause_stream()
                await _send_interaction_msg(update, context, chat_id, msg_text, buttons)

            logger.info(
                f"⏳ Awaiting user response for interaction {interaction_id}..."
            )
            result = await asyncio.wait_for(future, timeout=INTERACTION_TTL)
            logger.info(f"✅ User response received for {interaction_id}: {result}")
            return result

        except asyncio.TimeoutError:
            logger.error(f"⏱️ Interaction {interaction_id} timed out")
            PENDING_INTERACTIONS.pop(interaction_id, None)
            return False if kind == "permission" else "cancel"
        except Exception as e:
            logger.error(f"❌ Interaction {interaction_id} failed: {e}", exc_info=True)
            PENDING_INTERACTIONS.pop(interaction_id, None)
            return False if kind == "permission" else "cancel"

    # ---- Execute chat ----

    _streaming_token = streaming_mode.set(service.streaming_enabled)
    try:
        await service.chat(
            user_text,
            content_callback=stream_content,
            status_callback=tool_log,
            interaction_callback=interaction_callback,
            completion_callback=on_completion,
            delta_callback=stream_delta_callback if service.streaming_enabled else None,
            attachments=attachments,
        )

        # Wait for completion signal
        try:
            # SDK fires SESSION_IDLE after all tool chains complete; 5s allows
            # for multi-step tool chains and compaction events before we finalize.
            await asyncio.wait_for(completion_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Completion event timeout — proceeding")

        # Build footer
        footer = ""
        try:
            model = service.user_selected_model or service.current_model or "Auto"
            effort = service.current_reasoning_effort
            mode = service.agent_mode or "interactive"
            model_part = f"🤖 {model}"
            if effort:
                model_part += f" [{effort}]"
            footer = f"{model_part} · ⚙️ {mode}"
        except Exception as e:
            logger.error(f"Footer generation failed: {e}")

        # Finalize response — streaming edits live message, non-streaming sends new
        if service.streaming_enabled:
            await sender.finalize_stream(footer)
        else:
            full_response = "".join(response_chunks)
            await sender.send_response(full_response, footer)

        # Persist clean summary so /sessions shows meaningful titles
        session_id = getattr(getattr(service, "session_info", None), "session_id", None)
        if session_id and original_user_text:
            _write_session_summary(session_id, original_user_text)

    except asyncio.CancelledError:
        # /cancel was invoked — just dismiss the working message silently
        logger.info("Chat request cancelled by user via /cancel")
        await sender.delete_working()
    except asyncio.TimeoutError as e:
        error_msg = str(e)
        logger.error(f"Chat Timeout Error: {error_msg}")
        await sender.delete_working()
        # Flush any response accumulated before the timeout
        if service.streaming_enabled:
            await sender.finalize_stream()
        elif response_chunks:
            partial = "".join(response_chunks)
            if partial.strip():
                await sender.send_response(partial)
        await update.message.reply_text(
            "⚠️ Response timed out — partial result may appear above."
        )
    except Exception as e:
        logger.error(f"Chat Error: {e}")
        await sender.delete_working()
        # Auto-recover from sleep/wake session death
        if "Session not found" in str(e) or "-32603" in str(e):
            logger.info("🔄 Detected dead session, forcing reset...")
            try:
                service.session_expired = True
                await service.set_working_directory(service.get_working_directory())
                await update.message.reply_text(
                    "⚠️ Session was lost (sleep/wake?). Auto-recovered — please resend your message."
                )
            except Exception as recovery_err:
                logger.error(f"Recovery failed: {recovery_err}")
                await update.message.reply_text(
                    "⚠️ Session lost and recovery failed. Please use /start to reconnect."
                )
        else:
            await update.message.reply_text(f"⚠️ Error: {str(e)}")
    finally:
        streaming_mode.reset(_streaming_token)


async def _send_interaction_msg(update, context, chat_id, text, buttons):
    """Send an inline-keyboard message, with fallback to context.bot.send_message."""
    markup = InlineKeyboardMarkup(buttons)
    try:
        await update.message.reply_text(text, reply_markup=markup)
    except Exception as send_err:
        logger.error(
            f"❌ Failed to send interaction message: {send_err}", exc_info=True
        )
        if chat_id and context:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=markup
                )
            except Exception as fallback_err:
                logger.error(
                    f"❌ Fallback send also failed: {fallback_err}", exc_info=True
                )
                raise
        else:
            raise
