"""Shared constants and helpers for Copilot instructions file paths."""
from pathlib import Path
import logging
import re

logger = logging.getLogger(__name__)

USER_INSTRUCTIONS_PATH = Path.home() / ".copilot" / "copilot-instructions.md"
PROJECT_INSTRUCTIONS_RELPATH = Path(".github") / "copilot-instructions.md"
EMPTY_FILE_SENTINEL = "[empty file]"


def project_instructions_path(cwd: str | None) -> Path | None:
    """Return the absolute project-level instructions path for the given cwd, or None."""
    return Path(cwd) / PROJECT_INSTRUCTIONS_RELPATH if cwd else None


def extract_summary(content: str) -> str:
    """Return a short summary from an instructions file.

    Prefers the 'description' field from YAML frontmatter if present,
    otherwise falls back to the first non-empty non-heading line of content.
    Returns "" if nothing useful is found.
    """
    stripped = content.strip()
    # Try YAML frontmatter description field
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            frontmatter = stripped[3:end]
            m = re.search(r"^description:\s*['\"]?(.+?)['\"]?\s*$", frontmatter, re.MULTILINE)
            if m:
                return m.group(1).strip()[:120]
    # Fallback: first non-empty, non-heading, non-frontmatter line outside code fences
    in_fence = False
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line and not line.startswith("#") and not line.startswith("---") and not line.startswith("applyTo:"):
            return line[:120]
    return ""


def safe_read_instructions(path: Path | None, allowed_root: Path | None) -> str | None:
    """Read an instructions file after validating it stays within allowed_root.

    Security: when the file is a symlink, the resolved target must also have a
    .md extension. This allows symlinked instructions (e.g. from a dotfiles repo)
    while preventing exfiltration of sensitive files like ~/.ssh/id_rsa.

    Returns the file contents (stripped), EMPTY_FILE_SENTINEL for empty files,
    or None if the file should be skipped (missing, symlink escape, unreadable).
    """
    if not path or not allowed_root or not path.is_file():
        return None
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(allowed_root.resolve()):
            logger.warning(f"Skipping instructions file: {path} resolves outside {allowed_root}")
            return None
        # Symlink target must be a .md file to prevent reading arbitrary files
        if path.is_symlink() and resolved.suffix.lower() != ".md":
            logger.warning(f"Skipping instructions symlink: {path} → {resolved} (not a .md file)")
            return None
    except (OSError, ValueError):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip() or EMPTY_FILE_SENTINEL
    except OSError as e:
        logger.warning(f"Could not read instructions file {path}: {e}")
        return None
