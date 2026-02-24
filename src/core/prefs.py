"""Persistent user preferences stored in ~/.copilot/telegram-bot-prefs.json."""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.service import CopilotService

logger = logging.getLogger(__name__)

PREFS_FILE = Path.home() / ".copilot" / "telegram-bot-prefs.json"

_PREF_KEYS = (
    "user_selected_model",
    "current_reasoning_effort",
    "selected_agent",
    "agent_mode",
    "allow_all_tools",
    "streaming_enabled",
    "infinite_sessions_enabled",
)


def load_prefs() -> dict[str, Any]:
    """Load preferences from disk. Returns empty dict if file absent or invalid."""
    try:
        if PREFS_FILE.exists():
            return json.loads(PREFS_FILE.read_text())
    except Exception as e:
        logger.warning(f"Could not load prefs: {e}")
    return {}


def apply_prefs(service: "CopilotService") -> None:
    """Apply saved preferences to a freshly initialised service instance."""
    prefs = load_prefs()
    for key in _PREF_KEYS:
        if key in prefs:
            setattr(service, key, prefs[key])
    if prefs:
        logger.info(f"Loaded user prefs: {list(prefs.keys())}")


def save_prefs(service: "CopilotService") -> None:
    """Persist current user preferences to disk (merges with existing data)."""
    # Read existing file first so keys managed by other modules (e.g. disabled_skills)
    # are not wiped when we overwrite only _PREF_KEYS.
    existing = load_prefs()
    existing.update({key: getattr(service, key) for key in _PREF_KEYS})
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        logger.warning(f"Could not save prefs: {e}")
