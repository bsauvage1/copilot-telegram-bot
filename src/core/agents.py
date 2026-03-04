"""Agent directory scanner and parser for ~/.copilot/agents/."""

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from copilot.client import CopilotClient

AGENTS_DIR = Path.home() / ".copilot" / "agents"

_cached_builtin_agent_keys: list[str] | None = None


async def get_builtin_agent_keys(client: "Any") -> list[str]:
    """Return built-in agent type keys from the CLI tool spec.

    Calls tools.list RPC and extracts the agent_type enum from the task tool's
    parameter schema. Result is cached for the lifetime of the process.

    Args:
        client: CopilotClient instance with an active connection.

    Returns:
        List of built-in agent type strings, e.g. ["explore", "task", ...].
    """
    global _cached_builtin_agent_keys
    if _cached_builtin_agent_keys is not None:
        return _cached_builtin_agent_keys
    try:
        from copilot.generated.rpc import ToolsListParams

        result = await client.rpc.tools.list(ToolsListParams(model=None))
        for tool in result.tools:
            if tool.name == "task" and tool.parameters:
                props = tool.parameters.get("properties", {})
                agent_type_schema = props.get("agent_type", {})
                keys = agent_type_schema.get("enum", [])
                if keys:
                    _cached_builtin_agent_keys = [str(k) for k in keys]
                    return _cached_builtin_agent_keys
    except Exception:
        pass
    return []

# Ordered: first match wins. Checked against lowercased key+name+description.
_ICON_RULES = [
    (["janitor", "cleanup", "clean up", "tech debt", "simplif"],                  "🧹"),
    (["debug", "bug", "fix a bug", "diagnos"],                                    "🐛"),
    (["tdd", "test-first", "failing test"],                                       "🧪"),
    (["plan", "planning", "blueprint"],                                           "📋"),
    (["refactor", "improve code quality"],                                        "♻️"),
    (["\\bci\\b", "\\bcd\\b", "actions", "pipeline", "workflow", "deploy"],       "⚙️"),
    (["security", "owasp", "vulnerability", "exploit", "threat"],                 "🛡️"),
    (["principal", "senior", "engineering excellence", "leadership"],             "🏛️"),
    (["review", "audit", "inspect"],                                              "👁️"),
    (["document", "readme", "technical writ"],                                    "📝"),
    (["performance", "optimis", "optimiz", "latency"],                            "⚡"),
    (["database", "\\bsql\\b", "migration", "schema"],                            "🗄️"),
    (["infra", "terraform", "kubernetes", "\\bcloud\\b", "\\biac\\b"],            "☁️"),
]


def _extract_field(content: str, field: str) -> Optional[str]:
    match = re.search(rf"^{re.escape(field)}:\s*['\"]?([^\n'\"]*?)['\"]?\s*$", content, re.MULTILINE)
    return match.group(1).strip() if match else None


def _agent_key(path: Path) -> str:
    """Return the CLI-compatible agent key (strip .agent suffix if present)."""
    stem = path.stem  # e.g. "janitor.agent" or "janitor"
    return stem[:-6] if stem.endswith(".agent") else stem


def agent_icon(key: str, name: str, description: str) -> str:
    """Derive a meaningful emoji from the agent's own metadata."""
    corpus = f"{key} {name} {description}".lower()
    for keywords, icon in _ICON_RULES:
        if any(re.search(kw, corpus) for kw in keywords):
            return icon
    return "🤖"


def get_available_agents() -> list[dict]:
    """Scan AGENTS_DIR and return list of {key, name, description, icon} dicts."""
    if not AGENTS_DIR.exists():
        return []
    agents = []
    for f in sorted(AGENTS_DIR.iterdir()):
        if f.suffix != ".md":
            continue
        content = f.read_text(errors="replace")
        key = _agent_key(f)
        name = _extract_field(content, "name") or key
        description = _extract_field(content, "description") or ""
        model = _extract_field(content, "model") or ""
        agents.append({
            "key": key,
            "name": name,
            "description": description,
            "icon": agent_icon(key, name, description),
            "model": model,
        })
    return agents


def parse_agent_prompt(key: str) -> Optional[str]:
    """Return the body prompt of an agent file by key, or None if not found."""
    if not AGENTS_DIR.exists():
        return None
    for f in AGENTS_DIR.iterdir():
        if f.suffix == ".md" and _agent_key(f) == key:
            content = f.read_text(errors="replace")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip() or None
                # Malformed frontmatter (no closing ---) — reject rather than
                # return raw YAML-like text as the prompt
                return None
            return content.strip() or None
    return None
