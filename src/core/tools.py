import os
import stat
import time
import logging
from collections import deque
from pathlib import Path
from pydantic import BaseModel, Field
from copilot.tools import define_tool
from src.core.context import ctx
from src.config import FILE_CONTENT_LIMIT

logger = logging.getLogger(__name__)

# Rate-limiting state for notify_user tool
_notify_timestamps: deque[float] = deque()
_NOTIFY_RATE_LIMIT = 10   # max notifications per window
_NOTIFY_WINDOW = 60.0     # seconds
_NOTIFY_PREFIX = "🤖 [Copilot]\n"
_NOTIFY_MAX_MSG = 3900    # leaves room for prefix + Telegram overhead

# --- Tool Definitions ---


class ListFilesParams(BaseModel):
    path: str = Field(
        description="The directory path to list files from. Defaults to current directory ('.')."
    )


@define_tool(description="List files and directories in the project.")
async def list_files(params: ListFilesParams) -> str:
    target_path = Path(params.path)
    root = ctx.root_path

    if target_path.is_absolute():
        abs_target = target_path.resolve()
    else:
        abs_target = (root / target_path).resolve()

    # Security Check: Ensure path is within root (using Path.relative_to for robustness)
    try:
        abs_target.relative_to(root)
    except ValueError:
        return "Error: Access denied. Cannot access files outside workspace."

    try:
        items = os.listdir(abs_target)
        # Removed ctx.report_status() - SDK handles this via TOOL_EXECUTION_COMPLETE

        formatted_items = []
        for item in items:
            if (abs_target / item).is_dir():
                formatted_items.append(f"{item}/")
            else:
                formatted_items.append(item)
        return "\n".join(sorted(formatted_items))
    except Exception as e:
        return f"Error listing files: {str(e)}"


class NotifyUserParams(BaseModel):
    message: str = Field(description="Notification message to proactively send to the user.")


@define_tool(description="Send a proactive notification to the user's Telegram chat without waiting for their next message.")
async def notify_user(params: NotifyUserParams) -> str:
    if not ctx.notify_callback:
        return "Error: Notify callback not configured."
    # Rate limit: reject if too many notifications sent recently.
    now = time.monotonic()
    while _notify_timestamps and now - _notify_timestamps[0] > _NOTIFY_WINDOW:
        _notify_timestamps.popleft()
    if len(_notify_timestamps) >= _NOTIFY_RATE_LIMIT:
        return (
            f"Error: Rate limit exceeded "
            f"({_NOTIFY_RATE_LIMIT} notifications per {int(_NOTIFY_WINDOW)}s)."
        )
    _notify_timestamps.append(now)
    # Prefix so users can distinguish AI-originated messages from system alerts.
    # Truncate to stay within Telegram's message size limit.
    body = params.message[:_NOTIFY_MAX_MSG]
    message = f"{_NOTIFY_PREFIX}{body}"
    try:
        await ctx.notify_callback(message)
        return "Notification sent."
    except Exception as e:
        logger.error(f"notify_user failed: {e}")
        return f"Error: {e}"


class WorkspaceReadFileParams(BaseModel):
    path: str = Field(description="The relative path of the file to read.")


@define_tool(description="Read the content of a workspace file.")
async def workspace_read_file(params: WorkspaceReadFileParams) -> str:
    target_path = Path(params.path)
    root = ctx.root_path

    if target_path.is_absolute():
        abs_target = target_path.resolve()
    else:
        abs_target = (root / target_path).resolve()

    # Security Check: Ensure path is within root (using Path.relative_to for robustness)
    try:
        abs_target.relative_to(root)
    except ValueError:
        return "Error: Access denied. Cannot read files outside workspace."

    try:
        if not abs_target.exists():
            return f"Error: File not found: {params.path}"

        # Security: reject special files (FIFOs, device nodes, etc.) that could
        # block the event loop indefinitely when read synchronously.
        stat_result = abs_target.stat()
        if not stat.S_ISREG(stat_result.st_mode):
            return "Error: Not a regular file (special/device files are not supported)."

        with open(abs_target, "r", encoding="utf-8") as f:
            lines = f.readlines()
            content = "".join(lines)

            # Removed ctx.report_status() - SDK handles this via TOOL_EXECUTION_COMPLETE
            ctx.track_file(params.path)

            if len(content) > FILE_CONTENT_LIMIT:
                return content[:FILE_CONTENT_LIMIT] + "\n... (File truncated)"
            return content
    except UnicodeDecodeError:
        return "Error: Binary or unsupported file encoding."
    except Exception as e:
        return f"Error reading file: {str(e)}"
