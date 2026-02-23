"""MCP server config helpers — read/write ~/.copilot/mcp-config.json."""
import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

MCP_CONFIG_PATH = Path.home() / ".copilot" / "mcp-config.json"

_BUILTIN_SERVERS = {
    "github-mcp-server": {"type": "http", "url": "https://api.individual.githubcopilot.com/mcp/readonly"},
}


def load_config() -> Dict[str, Any]:
    """Load mcp-config.json. Returns {'mcpServers': {}, 'disabled': []}."""
    if not MCP_CONFIG_PATH.exists():
        return {"mcpServers": {}, "disabled": []}
    try:
        data = json.loads(MCP_CONFIG_PATH.read_text())
        data.setdefault("mcpServers", {})
        data.setdefault("disabled", [])
        return data
    except Exception as e:
        logger.warning(f"Failed to read mcp-config.json: {e}")
        return {"mcpServers": {}, "disabled": []}


def save_config(data: Dict[str, Any]) -> None:
    """Write mcp-config.json."""
    MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MCP_CONFIG_PATH.write_text(json.dumps(data, indent=2))


def get_enabled_servers() -> Dict[str, Any]:
    """Return only enabled user-configured servers for passing to SessionConfig."""
    config = load_config()
    disabled = set(config.get("disabled", []))
    return {
        name: srv
        for name, srv in config["mcpServers"].items()
        if name not in disabled
    }


def toggle_server(name: str) -> bool:
    """Toggle a server's enabled state. Returns True if now enabled.

    Raises ValueError if mcp-config.json exists but is malformed (to avoid
    overwriting the user's file with an empty config).
    """
    if MCP_CONFIG_PATH.exists():
        try:
            config = json.loads(MCP_CONFIG_PATH.read_text())
        except Exception as e:
            raise ValueError(f"mcp-config.json is malformed — fix it before toggling: {e}")
        config.setdefault("mcpServers", {})
        config.setdefault("disabled", [])
    else:
        config = {"mcpServers": {}, "disabled": []}
    disabled: list = config["disabled"]
    if name in disabled:
        disabled.remove(name)
        enabled = True
    else:
        disabled.append(name)
        enabled = False
    save_config(config)
    return enabled


def get_builtin_servers() -> Dict[str, Any]:
    return _BUILTIN_SERVERS


async def query_tools(srv: Dict[str, Any], timeout: float = 5.0) -> Optional[List[str]]:
    """Query a server's tool list via the MCP protocol. Returns tool names or None on failure.

    For stdio servers: spawns the process, does MCP handshake, calls tools/list, kills it.
    For http servers: sends a POST tools/list to the URL.
    """
    kind = srv.get("type", "stdio")
    try:
        if kind in ("stdio", "local", None):
            return await _query_stdio_tools(srv, timeout)
        elif kind in ("http", "sse"):
            return await _query_http_tools(srv, timeout)
    except Exception as e:
        logger.warning(f"query_tools failed for {kind} server: {e}")
    return None


async def _query_stdio_tools(srv: Dict[str, Any], timeout: float) -> Optional[List[str]]:
    """Spawn stdio MCP server, handshake, get tools/list, kill."""
    command = srv.get("command")
    args = srv.get("args", [])
    env_extra = srv.get("env", {})
    cwd = srv.get("cwd")
    if not command:
        return None

    import os
    env = {**os.environ, **env_extra}

    proc = await asyncio.create_subprocess_exec(
        command, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=cwd,
    )

    async def rpc(proc, req_id: int, method: str, params: Optional[dict] = None) -> dict:
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        proc.stdin.write((msg + "\n").encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        return json.loads(line.decode())

    async def notify(proc, method: str, params: Optional[dict] = None):
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        proc.stdin.write((msg + "\n").encode())
        await proc.stdin.drain()

    try:
        await rpc(proc, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "copilot-tg-bot", "version": "1.0"},
        })
        await notify(proc, "notifications/initialized")
        resp = await rpc(proc, 2, "tools/list")
        tools = resp.get("result", {}).get("tools", [])
        return [t["name"] for t in tools if "name" in t]
    finally:
        try:
            proc.stdin.close()
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


async def _query_http_tools(srv: Dict[str, Any], timeout: float) -> Optional[List[str]]:
    """Query tools/list from an HTTP/SSE MCP server."""
    url = srv.get("url")
    if not url:
        return None
    headers = {**srv.get("headers", {}), "Content-Type": "application/json"}
    import urllib.request
    req_body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
    }).encode()
    req = urllib.request.Request(url, data=req_body, headers=headers, method="POST")
    loop = asyncio.get_running_loop()
    def _fetch():
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    resp = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=timeout)
    tools = resp.get("result", {}).get("tools", [])
    return [t["name"] for t in tools if "name" in t]
