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
        self._working_msg: Message | None = (
            None  # The "Working..." message to delete before final response
        )
        self._working_buf: str = ""  # Accumulated tool-event text (streaming mode)
        self._stream_msg: Message | None = None  # Live-edited streaming message
        self._stream_buf: str = ""  # Accumulated streaming text
        self._stream_last_edit: float = 0.0  # Timestamp of last edit
        self._stream_creating: bool = False  # Guard: a task is creating _stream_msg
        self._STREAM_DEBOUNCE = 1.0  # Minimum seconds between edits
        self._working_last_edit: float = 0.0  # Timestamp of last Working card edit
        self._working_first_pending: float = (
            0.0  # When the oldest unsent update arrived
        )
        self._WORKING_DEBOUNCE = 2.0  # Min seconds between Working card edits
        self._WORKING_MAX_STALE = 6.0  # Force update after this many seconds
        self._start_time: float = (
            _time_mod.monotonic()
        )  # For elapsed time in final response
        self._interaction_wait: float = (
            0.0  # Seconds spent waiting for user interaction
        )
        self._interaction_start: float | None = None  # When current interaction started

    async def send_tool_event(self, detail: str):
        """Send a separate permanent message for each tool event."""
        await self._send_message(detail)

    async def update_working(self, detail: str):
        """Append tool status to the Working... card (streaming mode).
        Accumulates all events and shows a tail window so the card never exceeds
        Telegram's limit. Debounced to avoid Telegram rate-limit on edits.
        When streaming is live, events are still buffered (for finalize_working_card)
        but we skip editing since the streaming card is the active display."""
        self._working_buf += ("\n" if self._working_buf else "") + detail

        # Cap buffer to prevent unbounded growth during long sessions
        if len(self._working_buf) > self._STREAM_PREVIEW_LIMIT:
            self._working_buf = self._working_buf[-self._STREAM_PREVIEW_LIMIT :]

        # If the streaming card is live, the Working card is not the active
        # display — skip editing; finalize_working_card will show the log.
        if self._stream_msg:
            logger.debug(f"update_working: buffered (stream live): {detail[:60]}")
            return

        # Show tail so the card stays within Telegram's limit
        preview = self._working_buf
        if len(preview) > self._STREAM_PREVIEW_LIMIT:
            preview = "…\n" + preview[-self._STREAM_PREVIEW_LIMIT :]

        safe = html_lib.escape(preview)
        now = _time_mod.monotonic()
        if self._working_msg:
            # Debounce: skip the edit if we edited too recently,
            # but force an update after _WORKING_MAX_STALE seconds since the
            # first pending (unsent) event to avoid indefinitely stale displays.
            elapsed_since_edit = now - self._working_last_edit
            if elapsed_since_edit < self._WORKING_DEBOUNCE:
                if not self._working_first_pending:
                    self._working_first_pending = now
                if (now - self._working_first_pending) < self._WORKING_MAX_STALE:
                    logger.debug(
                        f"update_working: debounced ({elapsed_since_edit:.1f}s < {self._WORKING_DEBOUNCE}s)"
                    )
                    return
            self._working_last_edit = now
            self._working_first_pending = 0.0
            try:
                await asyncio.wait_for(
                    self._working_msg.edit_text(safe, parse_mode=ParseMode.HTML),
                    timeout=10.0,
                )
            except Exception as e:
                logger.debug(f"Could not update working message: {e}")
        else:
            self._working_last_edit = now
            self._working_first_pending = 0.0
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

    async def _finalize_working_card(self):
        """Edit the Working card to show the final tool-event log.

        If there were no tool events (just the initial 'Working...'), delete it
        instead of leaving a meaningless card.
        """
        if not self._working_msg:
            return
        if not self._working_buf.strip():
            # No tool events accumulated — just delete the bare 'Working...' card
            await self.delete_working()
            return
        # Finalize: just show the tool-event log (no elapsed footer — avoids
        # Telegram re-focusing the Working card instead of the new response below).
        final = self._working_buf
        if len(final) > self._STREAM_PREVIEW_LIMIT:
            final = "…\n" + final[-self._STREAM_PREVIEW_LIMIT :]
        try:
            safe = html_lib.escape(final)
            await asyncio.wait_for(
                self._working_msg.edit_text(safe, parse_mode=ParseMode.HTML),
                timeout=10.0,
            )
        except Exception as e:
            logger.debug(f"Could not finalize working card: {e}")
        finally:
            self._working_msg = None

    # Max chars to show in a live streaming edit (leave headroom for HTML escape overhead)
    _STREAM_PREVIEW_LIMIT = 3200

    async def stream_delta(self, chunk: str):
        """Accumulate streaming delta and edit Telegram message at most once per second.

        Shows a rolling tail window of the last _STREAM_PREVIEW_LIMIT chars during live
        editing so we never exceed Telegram's 4096-char message limit mid-stream.

        Delays creating the streaming card until we have meaningful visible content,
        so that early tool-use deltas (JSON fragments) don't suppress the Working card.

        Uses _stream_creating guard to prevent duplicate card creation.
        _dispatch_async fires each delta as a separate asyncio.Task; without the
        guard, multiple tasks can see _stream_msg as None during the first
        send_message await and each create a new Telegram message.
        """
        self._stream_buf += chunk
        now = _time_mod.monotonic()
        if now - self._stream_last_edit < self._STREAM_DEBOUNCE:
            return

        # Don't transition to streaming card until we have visible content (not just
        # JSON fragments from tool-use deltas).  20 chars catches short replies like
        # "Yes, I'll do that." that the previous 50-char threshold would suppress.
        visible = self._stream_buf.strip()
        if not self._stream_msg and len(visible) < 20:
            logger.debug(f"stream_delta: waiting for content ({len(visible)} chars)")
            return

        # Build preview: only the response text (tool events stay in the Working card)
        preview = self._stream_buf
        if len(preview) > self._STREAM_PREVIEW_LIMIT:
            preview = "…" + preview[-self._STREAM_PREVIEW_LIMIT :]

        if not self._stream_msg:
            # Guard: another task is already creating the message — skip.
            # The chunk is buffered in _stream_buf and will appear in the next edit.
            if self._stream_creating:
                return
            self._stream_creating = True
            # Resume: record how long the interaction wait took
            if self._interaction_start is not None:
                self._interaction_wait += (
                    _time_mod.monotonic() - self._interaction_start
                )
                self._interaction_start = None
            # Keep the Working card as a permanent log — create a NEW message
            # for streaming content (don't reuse _working_msg).
            try:
                safe = html_lib.escape(preview)
                self._stream_msg = await self.chat.send_message(
                    safe, parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.debug(f"Stream start failed: {e}")
            finally:
                self._stream_creating = False
                self._stream_last_edit = _time_mod.monotonic()
        else:
            try:
                safe = html_lib.escape(preview)
                await asyncio.wait_for(
                    self._stream_msg.edit_text(safe, parse_mode=ParseMode.HTML),
                    timeout=10.0,
                )
                self._stream_last_edit = _time_mod.monotonic()
            except RetryAfter as e:
                # Advance the debounce clock by the full mandatory backoff so that
                # subsequent deltas don't immediately retry and hammer the endpoint.
                logger.debug(f"Stream edit rate-limited ({e.retry_after}s)")
                self._stream_last_edit = (
                    _time_mod.monotonic() + e.retry_after - self._STREAM_DEBOUNCE
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    logger.debug(f"Stream edit failed: {e}")
                self._stream_last_edit = _time_mod.monotonic()
            except Exception as e:
                logger.debug(f"Stream edit failed: {e}")
                self._stream_last_edit = _time_mod.monotonic()

    async def finalize_stream(self, footer: str = ""):
        """Finalize the Working card and send the full response as new messages."""
        msg = self._stream_msg
        text = self._stream_buf
        self._stream_msg = None
        self._stream_buf = ""

        # Close any open interaction timer before computing elapsed
        if self._interaction_start is not None:
            self._interaction_wait += _time_mod.monotonic() - self._interaction_start
            self._interaction_start = None

        elapsed = max(
            0.0, _time_mod.monotonic() - self._start_time - self._interaction_wait
        )
        elapsed_str = f"⏱ {elapsed:.1f}s"

        # Finalize the Working card: replace with the tool-event log
        await self._finalize_working_card()

        if not text or not text.strip():
            if msg:
                try:
                    await asyncio.wait_for(msg.delete(), timeout=2.0)
                except Exception:
                    pass
            return

        if footer:
            full = f"{text}\n\n---\n{footer}\n{elapsed_str}"
        else:
            full = f"{text}\n\n{elapsed_str}"

        chunks = self._split_message(full)
        if not chunks:
            chunks = ["_(empty response)_"]

        # For single-chunk responses, edit the live card in place — avoids the
        # "card disappears then reappears" flash that delete+resend causes.
        if msg and len(chunks) == 1:
            try:
                safe = html_lib.escape(chunks[0])
                await asyncio.wait_for(
                    msg.edit_text(safe, parse_mode=ParseMode.HTML),
                    timeout=10.0,
                )
                return
            except Exception:
                pass  # Fall through to delete + resend

        # Multi-chunk or edit failed: delete the preview and send as new messages
        if msg:
            try:
                await asyncio.wait_for(msg.delete(), timeout=2.0)
            except Exception:
                pass

        for chunk in chunks:
            safe = html_lib.escape(chunk)
            await self._safe_send_html(safe)

    async def send_response(self, text: str, footer: str = ""):
        """Send the final model response (with footer). Auto-splits long messages.

        Finalizes the Working card (keeps it as a permanent log), then sends
        response chunks as new messages below it.
        """
        # Close any open interaction timer before computing elapsed
        if self._interaction_start is not None:
            self._interaction_wait += _time_mod.monotonic() - self._interaction_start
            self._interaction_start = None

        elapsed = max(
            0.0, _time_mod.monotonic() - self._start_time - self._interaction_wait
        )
        elapsed_str = f"⏱ {elapsed:.1f}s"

        # Finalize the Working card
        await self._finalize_working_card()

        if footer:
            full = f"{text}\n\n---\n{footer}\n{elapsed_str}"
        else:
            full = f"{text}\n\n{elapsed_str}"

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
                if in_code_block:
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
            return
        except RetryAfter as e:
            if _retry_count >= 3:
                logger.warning("⏱️ edit_message max retries reached — skipping")
                return
            await asyncio.sleep(e.retry_after)
            await self._edit_message(message, text, _retry_count + 1)
            return  # don't fall through to plain-text fallback after a successful retry
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return  # not an error — message already has this content
            elif "Can't parse entities" in str(e):
                try:
                    await asyncio.wait_for(
                        message.edit_text(
                            html_lib.escape(text), parse_mode=ParseMode.HTML
                        ),
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
        # Last-resort: plain text edit
        try:
            await asyncio.wait_for(
                message.edit_text(text),
                timeout=10.0,
            )
        except Exception as e:
            logger.error(f"❌ edit_message plain-text fallback failed: {e}")

    async def _safe_send(self, text: str, _retry_count: int = 0) -> Message | None:
        """Core send logic with retry, markdown fallback, and error handling.

        Returns the sent Message (or None on failure / fire-and-forget).
        Plain-text fallback only fires on definite API errors, not timeouts,
        to avoid duplicate messages when the original send may have succeeded.
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
                # Plain-text fallback for parse failures (definite API rejection)
                try:
                    return await asyncio.wait_for(
                        self.chat.send_message(text),
                        timeout=10.0,
                    )
                except Exception:
                    logger.warning("Failed to send message as plain text")
            else:
                logger.error(f"❌ send_message failed: {e}")
        except asyncio.TimeoutError:
            # Don't fall back — message may have been delivered; avoid duplicates
            logger.warning("⏱️ send_message timeout — skipping")
            return None
        except Exception as e:
            logger.error(f"❌ send_message error: {e}")
            # Plain-text fallback for definite local errors only
            try:
                return await asyncio.wait_for(
                    self.chat.send_message(text),
                    timeout=10.0,
                )
            except Exception as fe:
                logger.error(f"❌ send_message plain-text fallback failed: {fe}")
        return None

    async def _safe_send_html(self, html_text: str) -> Message | None:
        """Send a pre-escaped HTML message, with plain-text fallback on parse error."""
        try:
            return await asyncio.wait_for(
                self.chat.send_message(html_text, parse_mode=ParseMode.HTML),
                timeout=10.0,
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return await self._safe_send_html(html_text)
        except BadRequest as e:
            logger.warning(f"HTML send failed ({e}), falling back to plain text")
            try:
                return await asyncio.wait_for(
                    self.chat.send_message(html_text),
                    timeout=10.0,
                )
            except Exception as fe:
                logger.error(f"❌ _safe_send_html plain-text fallback failed: {fe}")
        except asyncio.TimeoutError:
            logger.warning("⏱️ _safe_send_html timeout — skipping")
        except Exception as e:
            logger.error(f"❌ _safe_send_html error: {e}")
        return None

    async def _send_message(self, text: str, _retry_count: int = 0):
        """Send a new message to the chat (fire-and-forget)."""
        await self._safe_send(text, _retry_count)

    async def _send_message_return(
        self, text: str, _retry_count: int = 0
    ) -> Message | None:
        """Send a new message and return the Message object."""
        return await self._safe_send(text, _retry_count)
