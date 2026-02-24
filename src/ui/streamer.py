import asyncio
import html as html_lib
import logging
import time as _time_mod
from telegram import Message, Chat
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest

from src.config import TELEGRAM_MSG_LIMIT

logger = logging.getLogger(__name__)


class MessageSender:
    """
    Sends blocking (non-streaming) messages to Telegram.

    Design:
    - Tool events create separate permanent messages
    - "Working..." message shown at the top after user sends message
    - "Working..." deleted when final response is ready
    - Final response sent as new messages
    - Messages auto-split at 4000 chars with footer appended
    """

    PAGE_LIMIT = TELEGRAM_MSG_LIMIT  # Telegram's actual limit is 4096

    def __init__(self, message: Message):
        self.chat: Chat = message.chat
        self._working_msg: Message | None = None  # The "Working..." message to delete before final response
        self._working_buf: str = ""               # Accumulated tool-event text (streaming mode)
        self._stream_msg: Message | None = None   # Live-edited streaming message
        self._stream_buf: str = ""                # Accumulated streaming text
        self._stream_last_edit: float = 0.0       # Timestamp of last edit
        self._STREAM_DEBOUNCE = 1.0               # Minimum seconds between edits
        self._start_time: float = _time_mod.monotonic()  # For elapsed time in final response
        self._interaction_wait: float = 0.0               # Seconds spent waiting for user interaction
        self._interaction_start: float | None = None      # When current interaction started

    async def send_tool_event(self, detail: str):
        """Send a separate permanent message for each tool event."""
        await self._send_message(detail)

    async def update_working(self, detail: str):
        """Append tool status to the Working... card (streaming mode).
        Accumulates all events and shows a tail window so the card never exceeds
        Telegram's limit. Once the streaming card is live, events are silently dropped."""
        if self._stream_msg:
            logger.debug(f"Stream live — dropping tool event: {detail[:80]}")
            return

        self._working_buf += ("\n" if self._working_buf else "") + detail

        # Show tail so the card stays within Telegram's limit
        preview = self._working_buf
        if len(preview) > self._STREAM_PREVIEW_LIMIT:
            preview = "…\n" + preview[-self._STREAM_PREVIEW_LIMIT:]

        safe = html_lib.escape(preview)
        if self._working_msg:
            try:
                await asyncio.wait_for(
                    self._working_msg.edit_text(safe, parse_mode=ParseMode.HTML),
                    timeout=10.0,
                )
            except Exception as e:
                logger.debug(f"Could not update working message: {e}")
        else:
            try:
                self._working_msg = await self._send_message_return(safe)
            except Exception as e:
                logger.warning(f"Failed to create working message: {e}")

    async def pause_stream(self):
        """Delete the live streaming card so the next stream_delta creates a fresh one
        after an interaction card (permission / input prompt) that is about to be sent.
        Clears _working_buf so stale tool events are not prepended to the resumed stream.
        Starts timing the interaction wait so it can be excluded from elapsed time."""
        if self._stream_msg:
            try:
                await self._stream_msg.delete()
            except Exception:
                pass
            self._stream_msg = None
        self._working_buf = ""
        self._interaction_start = _time_mod.monotonic()
    
    async def create_working(self):
        """Create 'Working...' message once at the start."""
        if not self._working_msg:
            try:
                self._working_msg = await self._send_message_return("⏳ Working...")
            except Exception as e:
                logger.warning(f"Failed to create working message: {e}")

    async def delete_working(self):
        """Delete the 'Working...' message if it exists."""
        if self._working_msg:
            try:
                await asyncio.wait_for(self._working_msg.delete(), timeout=2.0)
            except Exception as e:
                logger.debug(f"Could not delete working message: {e}")
            finally:
                self._working_msg = None

    # Max chars to show in a live streaming edit (leave headroom for escape overhead + cursor)
    _STREAM_PREVIEW_LIMIT = 3800

    async def stream_delta(self, chunk: str):
        """Accumulate streaming delta and edit Telegram message at most once per second.

        Shows a rolling tail window of the last _STREAM_PREVIEW_LIMIT chars during live
        editing so we never exceed Telegram's 4096-char message limit mid-stream.
        """
        self._stream_buf += chunk
        now = _time_mod.monotonic()
        if now - self._stream_last_edit < self._STREAM_DEBOUNCE:
            return
        self._stream_last_edit = now

        # Build preview: tool events followed by the response so far
        combined = (self._working_buf + "\n\n" + self._stream_buf) if self._working_buf else self._stream_buf
        if len(combined) > self._STREAM_PREVIEW_LIMIT:
            preview = "…" + combined[-self._STREAM_PREVIEW_LIMIT:]
        else:
            preview = combined

        if not self._stream_msg:
            # Resume: record how long the interaction wait took
            if self._interaction_start is not None:
                self._interaction_wait += _time_mod.monotonic() - self._interaction_start
                self._interaction_start = None
            # Transition Working... card into the streaming card (reuse it, don't delete)
            if self._working_msg:
                self._stream_msg = self._working_msg
                self._working_msg = None
            try:
                safe = html_lib.escape(preview) + " ✍️"
                if self._stream_msg:
                    await asyncio.wait_for(
                        self._stream_msg.edit_text(safe, parse_mode=ParseMode.HTML),
                        timeout=10.0,
                    )
                else:
                    self._stream_msg = await self.chat.send_message(safe, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.debug(f"Stream start failed: {e}")
        else:
            try:
                safe = html_lib.escape(preview) + " ✍️"
                await asyncio.wait_for(
                    self._stream_msg.edit_text(safe, parse_mode=ParseMode.HTML),
                    timeout=10.0,
                )
            except Exception as e:
                logger.debug(f"Stream edit failed: {e}")

    async def finalize_stream(self, footer: str = ""):
        """Replace streaming message with the full final content, split across pages if needed."""
        msg = self._stream_msg
        text = self._stream_buf
        self._stream_msg = None
        self._stream_buf = ""

        if not msg:
            return

        # Close any open interaction timer before computing elapsed
        if self._interaction_start is not None:
            self._interaction_wait += _time_mod.monotonic() - self._interaction_start
            self._interaction_start = None

        elapsed = max(0.0, _time_mod.monotonic() - self._start_time - self._interaction_wait)
        elapsed_str = f"\n\n⏱ {elapsed:.1f}s"

        full = text + elapsed_str
        if footer:
            full = text + elapsed_str + "\n\n---\n" + footer

        chunks = self._split_message(full)
        if not chunks:
            chunks = ["_(empty response)_"]

        # Edit the live streaming message in-place with the first chunk, then send the rest
        try:
            await self._edit_message(msg, self._ensure_safe_markdown(chunks[0]))
        except Exception:
            await self._safe_send(self._ensure_safe_markdown(chunks[0]))

        for chunk in chunks[1:]:
            await self._safe_send(self._ensure_safe_markdown(chunk))

    async def send_response(self, text: str, footer: str = ""):
        """Send the final model response (with footer). Auto-splits long messages.
        
        Deletes "Working..." message first, then sends all response chunks as new messages.
        """
        await self.delete_working()

        # Close any open interaction timer before computing elapsed
        if self._interaction_start is not None:
            self._interaction_wait += _time_mod.monotonic() - self._interaction_start
            self._interaction_start = None

        elapsed = max(0.0, _time_mod.monotonic() - self._start_time - self._interaction_wait)
        elapsed_str = f"\n\n⏱ {elapsed:.1f}s"

        full = text + elapsed_str
        if footer:
            full = text + elapsed_str + "\n\n---\n" + footer

        chunks = self._split_message(full)
        if not chunks:
            chunks = ["_(empty response)_"]

        # Send all chunks as new messages
        for chunk in chunks:
            safe = self._ensure_safe_markdown(chunk)
            await self._send_message(safe)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _split_message(self, text: str) -> list[str]:
        """Split text into chunks ≤ PAGE_LIMIT, closing/re-opening code blocks."""
        if len(text) <= self.PAGE_LIMIT:
            return [text]

        chunks: list[str] = []
        remaining = text
        in_code_block = False
        code_fence_lang = ""

        while remaining:
            if len(remaining) <= self.PAGE_LIMIT:
                chunks.append(remaining)
                break

            # Find a good break point near the limit
            limit = self.PAGE_LIMIT
            # Reserve space for closing a code block if needed
            if in_code_block:
                limit -= 5  # room for \n```

            cut = remaining[:limit]
            # Prefer breaking at double newline > newline > space
            break_at = cut.rfind("\n\n")
            if break_at < limit // 2:
                break_at = cut.rfind("\n")
            if break_at < limit // 2:
                break_at = cut.rfind(" ")
            if break_at < limit // 2:
                break_at = limit  # hard cut

            chunk = remaining[:break_at]
            remaining = remaining[break_at:].lstrip("\n")

            # Track code-block state: count triple-backtick occurrences in this chunk
            fences = chunk.split("```")
            # Number of ``` in chunk = len(fences) - 1
            fence_count = len(fences) - 1
            if fence_count % 2 != 0:
                in_code_block = not in_code_block
                # Find the language tag of the last opening fence if entering
                if in_code_block:
                    # Last fence piece is the content after the last ```
                    # The fence piece before it ends with the opening ``` line
                    last_fence_line = fences[-2].split("\n")[-1] if len(fences) >= 2 else ""
                    code_fence_lang = ""  # simplified — don't try to parse lang

            # Close unclosed code block at chunk boundary
            if in_code_block:
                chunk += "\n```"

            chunks.append(chunk)

            # Re-open code block in next chunk
            if in_code_block:
                remaining = f"```{code_fence_lang}\n" + remaining

        return chunks

    @staticmethod
    def _ensure_safe_markdown(text: str) -> str:
        """Close unclosed code blocks and inline code spans."""
        count = text.count("```")
        if count % 2 != 0:
            text += "\n```"
        if text.count("`") % 2 != 0 and "```" not in text[-5:]:
            text += "`"
        return text

    async def _edit_message(self, message: Message, text: str, _retry_count: int = 0):
        """Edit a Telegram message with markdown fallback."""
        try:
            await asyncio.wait_for(
                message.edit_text(text, parse_mode=ParseMode.MARKDOWN),
                timeout=10.0,
            )
        except RetryAfter as e:
            if _retry_count >= 3:
                logger.warning("⏱️ edit_message max retries reached — skipping")
                return
            await asyncio.sleep(e.retry_after)
            await self._edit_message(message, text, _retry_count + 1)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            elif "Can't parse entities" in str(e):
                try:
                    await asyncio.wait_for(
                        message.edit_text(html_lib.escape(text), parse_mode=ParseMode.HTML),
                        timeout=10.0,
                    )
                except Exception:
                    logger.warning("Failed to edit message even as plain text")
            else:
                logger.error(f"❌ edit_message failed: {e}")
        except asyncio.TimeoutError:
            logger.warning("⏱️ edit_message timeout — skipping")
        except Exception as e:
            logger.error(f"❌ edit_message error: {e}")

    async def _safe_send(self, text: str, _retry_count: int = 0) -> Message | None:
        """Core send logic with retry, markdown fallback, and error handling.

        Returns the sent Message (or None on failure / fire-and-forget).
        """
        try:
            return await asyncio.wait_for(
                self.chat.send_message(text, parse_mode=ParseMode.MARKDOWN),
                timeout=10.0,
            )
        except RetryAfter as e:
            if _retry_count >= 3:
                logger.warning("⏱️ send max retries reached — skipping")
                return None
            await asyncio.sleep(e.retry_after)
            return await self._safe_send(text, _retry_count + 1)
        except BadRequest as e:
            if "Can't parse entities" in str(e):
                try:
                    return await asyncio.wait_for(
                        self.chat.send_message(html_lib.escape(text), parse_mode=ParseMode.HTML),
                        timeout=10.0,
                    )
                except Exception:
                    logger.warning("Failed to send message even as plain text")
            else:
                logger.error(f"❌ send_message failed: {e}")
        except asyncio.TimeoutError:
            logger.warning("⏱️ send_message timeout — skipping")
        except Exception as e:
            logger.error(f"❌ send_message error: {e}")
        return None

    async def _send_message(self, text: str, _retry_count: int = 0):
        """Send a new message to the chat (fire-and-forget)."""
        await self._safe_send(text, _retry_count)

    async def _send_message_return(self, text: str, _retry_count: int = 0) -> Message | None:
        """Send a new message and return the Message object."""
        return await self._safe_send(text, _retry_count)
