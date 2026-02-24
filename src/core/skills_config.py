"""Skills config helpers — scan skill directories and manage disabled skills."""

import hashlib
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Set

from src.core.prefs import PREFS_FILE as SKILLS_PREFS_FILE  # shared prefs file

logger = logging.getLogger(__name__)

CLI_CONFIG_FILE = Path.home() / ".copilot" / "config.json"

# Well-known user-level skill directory (same default the CLI uses)
USER_SKILLS_DIR = Path.home() / ".copilot" / "skills"

# Telegram callback_data limit is 64 bytes. "skill_toggle:" = 13 bytes.
_CB_PREFIX = "skill_toggle:"


def _cli_skill_directories() -> List[Path]:
    """Read skill_directories registered by the CLI in ~/.copilot/config.json."""
    try:
        if CLI_CONFIG_FILE.exists():
            data = json.loads(CLI_CONFIG_FILE.read_text())
            return [Path(p) for p in data.get("skill_directories", []) if Path(p).is_dir()]
    except Exception as e:
        logger.warning(f"Could not read CLI config for skill_directories: {e}")
    return []


def get_user_skill_dirs() -> Set[Path]:
    """Return the set of user-owned skill dirs: USER_SKILLS_DIR + CLI-registered dirs.

    Public so consumers (e.g. build_skills_panel) can label dirs without re-reading
    config.json themselves.
    """
    return {USER_SKILLS_DIR} | set(_cli_skill_directories())


def _prefs() -> Dict[str, Any]:
    try:
        if SKILLS_PREFS_FILE.exists():
            return json.loads(SKILLS_PREFS_FILE.read_text())
    except Exception as e:
        logger.warning(f"Could not read prefs for skills: {e}")
    return {}


def _write_prefs(data: Dict[str, Any]) -> None:
    try:
        SKILLS_PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SKILLS_PREFS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(f"Could not write prefs for skills: {e}")


def get_disabled_skills() -> List[str]:
    return _prefs().get("disabled_skills", [])


def set_disabled_skills(names: List[str]) -> None:
    data = _prefs()
    data["disabled_skills"] = names
    _write_prefs(data)


def toggle_skill(name: str) -> bool:
    """Toggle a skill's disabled state. Returns True if now enabled."""
    disabled = get_disabled_skills()
    if name in disabled:
        disabled.remove(name)
        enabled = True
    else:
        disabled.append(name)
        enabled = False
    set_disabled_skills(disabled)
    return enabled


def get_skill_dirs(workspace_path: str | None = None) -> List[Path]:
    """Return ordered, deduplicated skill directories to scan.

    Order: CLI-registered dirs (from config.json) → USER_SKILLS_DIR (if not already
    included) → project-level .github/skills.
    """
    seen: set[Path] = set()
    dirs: List[Path] = []

    def _add(p: Path) -> None:
        if p not in seen and p.is_dir():
            seen.add(p)
            dirs.append(p)

    for p in _cli_skill_directories():
        _add(p)
    _add(USER_SKILLS_DIR)
    if workspace_path:
        _add(Path(workspace_path) / ".github" / "skills")

    return dirs


def scan_skills(workspace_path: str | None = None) -> List[Dict[str, str]]:
    """Scan skill directories and return list of skill dicts with name, path, source.

    Skill layout supported (CLI canonical format first):
      <dir>/<skill-name>/SKILL.md   ← primary (what the CLI uses)
      <dir>/<file>.md               ← flat fallback

    Unreadable directories are skipped gracefully.
    """
    skills = []
    seen_names: set[str] = set()
    user_dirs = get_user_skill_dirs()  # single read of config.json

    for skill_dir in get_skill_dirs(workspace_path):
        source = "user" if skill_dir in user_dirs else "project"
        try:
            entries = list(skill_dir.iterdir())
        except OSError as e:
            logger.warning(f"Skipping unreadable skill dir {skill_dir}: {e}")
            continue

        # Primary: subdir/SKILL.md
        for subdir in sorted(p for p in entries if p.is_dir()):
            skill_md = subdir / "SKILL.md"
            if skill_md.exists():
                name = _extract_skill_name(skill_md) or subdir.name
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                skills.append({"name": name, "path": str(skill_md), "source": source})

        # Fallback: flat *.md files
        for md_file in sorted(p for p in entries if p.suffix == ".md" and p.is_file()):
            name = _extract_skill_name(md_file) or md_file.stem
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            skills.append({"name": name, "path": str(md_file), "source": source})

    return skills


def _extract_skill_name(path: Path) -> str:
    """Extract skill name from frontmatter or fall back to filename stem."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                frontmatter = content[3:end]
                for line in frontmatter.splitlines():
                    if line.startswith("name:"):
                        return line[5:].strip().strip("'\"")
    except Exception:
        pass
    return path.stem


def get_skill_dirs_for_session(workspace_path: str | None = None) -> List[str]:
    """Return skill directory paths as strings for passing to SessionConfig."""
    return [str(d) for d in get_skill_dirs(workspace_path)]


def skill_callback_name(name: str) -> str:
    """Return a stable, collision-resistant, byte-safe callback key for a skill name.

    Uses an MD5 hex digest so the total callback_data is always exactly 45 bytes
    (13 prefix + 32 hex chars), well within Telegram's 64-byte limit regardless of
    multi-byte characters or name length.
    """
    return hashlib.md5(name.encode()).hexdigest()
