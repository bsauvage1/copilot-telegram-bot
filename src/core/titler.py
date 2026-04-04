"""Async session title generation via a one-shot Copilot SDK session."""

import asyncio
import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_TITLE_PROMPT = (
    "Reply with ONLY a 5-7 word title (no punctuation, no quotes) that describes "
    "what this conversation is about: {message}"
)

# Fixed prefix used by both the SDK's own title mechanism and this bot's titler.
# Used to detect and strip the prompt wrapper from first user messages.
_TITLE_PROMPT_PREFIX = (
    "Reply with ONLY a 5-7 word title (no punctuation, no quotes) that describes "
    "what this conversation is about: "
)

# Guard: only one title-generation task per session_id at a time
_in_flight: set[str] = set()


def read_first_user_message(session_id: str) -> str | None:
    """Return the first real user message from a session's events.jsonl.

    Strips the system-prompt preamble that precedes the `---` separator the
    Telegram bot injects at the top of every user turn.
    """
    events_file = (
        Path.home() / ".copilot" / "session-state" / session_id / "events.jsonl"
    )
    try:
        for line in events_file.read_text().splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "user.message":
                continue
            content: str = event.get("data", {}).get("content", "") or ""
            # Strip the SDK's own title-generation prompt wrapper to recover
            # the real user message embedded at the end of the prompt.
            if content.startswith(_TITLE_PROMPT_PREFIX):
                content = content[len(_TITLE_PROMPT_PREFIX) :]
            # Strip system-prompt preamble (everything up to and including "---\n")
            if "---\n" in content:
                content = content.split("---\n", 1)[-1]
            content = content.strip()
            if content:
                return content
    except Exception:
        pass
    return None


def _has_summary(session_id: str) -> bool:
    """Return True if workspace.yaml has a clean, AI-generated summary.

    Returns False when the stored summary is absent, noisy, or is just the
    raw first user message written by the SDK before title generation ran.
    """
    from src.ui.menus import _clean_summary

    raw = read_session_summary(session_id)
    if not _clean_summary(raw):
        return False
    # Reject summaries that are the SDK's title-generation prompt (stored
    # verbatim before the real title could be written back).
    if raw and raw.lstrip().startswith("Reply with ONLY"):
        return False
    # Reject summaries that are just the SDK-written first user message
    first_msg = read_first_user_message(session_id)
    if first_msg and raw and raw.strip().lower() == first_msg.strip().lower():
        return False
    return True


_YAML_BLOCK_SCALARS = frozenset({"|-", "|+", "|", ">-", ">+", ">"})


def read_session_summary(session_id: str) -> str | None:
    """Return the summary value from workspace.yaml, or None if absent/empty.

    Returns None when the value is a YAML block scalar indicator (the actual
    content lives on subsequent indented lines and is not parsed here).
    """
    workspace = (
        Path.home() / ".copilot" / "session-state" / session_id / "workspace.yaml"
    )
    try:
        for line in workspace.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("summary:"):
                value = stripped[len("summary:") :].strip().strip('"').strip("'")
                if value in _YAML_BLOCK_SCALARS:
                    return None  # block scalar — content on next lines
                return value or None
    except Exception:
        pass
    return None


async def _run_title_generation(session_id: str, first_message: str) -> None:
    """Create a minimal one-shot SDK session, generate a title, write it back."""
    from src.core.service import service
    from src.ui.menus import write_session_summary

    prompt = _TITLE_PROMPT.format(message=first_message[:300])
    title: str | None = None
    temp_session = None
    try:
        from copilot.session import PermissionHandler

        temp_session = await service.client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            streaming=False,
            tools=[],
        )
        event = await temp_session.send_and_wait(prompt, timeout=60)
        raw = ""
        if event is not None:
            raw = (
                (getattr(event.data, "content", None) or "")
                .strip()
                .strip('"')
                .strip("'")
            )
        title = raw[:80] if raw else None
    except Exception as e:
        logger.warning(f"Title generation failed for {session_id}: {e}")
    finally:
        if temp_session is not None:
            temp_session_id = getattr(temp_session, "session_id", None)
            try:
                await temp_session.destroy()
            except Exception:
                pass
            if temp_session_id:
                session_dir = (
                    Path.home() / ".copilot" / "session-state" / temp_session_id
                )
                shutil.rmtree(session_dir, ignore_errors=True)
                logger.debug(
                    f"Cleaned up temp title-gen session dir: {temp_session_id[-8:]}"
                )
        _in_flight.discard(session_id)

    if title:
        write_session_summary(session_id, title)
        logger.info(f"Generated title for {session_id}: {title!r}")


def schedule_title_generation(session_id: str, first_message: str = "") -> None:
    """Fire-and-forget: generate and persist a session title in the background.

    Safe to call multiple times — only one task runs per session_id at a time,
    and skips silently if a summary already exists.

    ``first_message`` is ignored; the actual first user message is always read
    from events.jsonl so callers cannot accidentally pass the wrong turn.
    """
    if not session_id:
        return
    if _has_summary(session_id):
        return
    if session_id in _in_flight:
        return
    msg = read_first_user_message(session_id)
    if not msg:
        return
    _in_flight.add(session_id)
    asyncio.create_task(
        _run_title_generation(session_id, msg),
        name=f"title-gen-{session_id[-8:]}",
    )
    logger.debug(f"Scheduled title generation for {session_id[-8:]}")


async def get_or_generate_summary(session_id: str) -> str:
    """Return session summary, awaiting generation if not yet available.

    Unlike schedule_title_generation, this awaits the LLM call so the
    caller always gets the real summary (or "" if generation fails/no msg).
    """
    if not session_id:
        return ""
    summary = read_session_summary(session_id)
    if summary:
        return summary
    first_msg = read_first_user_message(session_id)
    if not first_msg:
        return ""
    if session_id not in _in_flight:
        _in_flight.add(session_id)
        await _run_title_generation(session_id, first_msg)
    return read_session_summary(session_id) or ""


async def ensure_session_titles(sessions: list) -> None:
    """Await title generation for all sessions that are missing a summary.

    Runs generations sequentially to avoid competing for the Copilot API
    under load (parallel requests frequently cause timeouts).
    Sessions that already have a summary are skipped instantly.
    """
    pending: list[tuple[str, str]] = []
    for s in sessions:
        session_id: str = getattr(s, "sessionId", None) or ""
        if not session_id:
            continue
        if _has_summary(session_id):
            continue
        if session_id in _in_flight:
            continue
        first_msg = read_first_user_message(session_id)
        if not first_msg:
            continue
        pending.append((session_id, first_msg))

    if pending:
        logger.info(f"Generating titles for {len(pending)} session(s)...")
        for session_id, first_msg in pending:
            _in_flight.add(session_id)
            await _run_title_generation(session_id, first_msg)
