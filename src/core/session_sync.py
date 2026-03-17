"""Remote session sync client — reads from a copilot-sessions GitHub repo."""

import base64
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from src.config import COPILOT_SESSIONS_REPO, GITHUB_TOKEN

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_cached_sync_client: Optional["SessionSyncClient"] = None


@dataclass
class RemoteSession:
    """SDK-compatible session object representing a remote (un-pulled) session."""

    sessionId: str
    summary: Optional[str] = None
    startTime: Optional[str] = None
    modifiedTime: Optional[str] = None
    cwd: Optional[str] = None
    machine: Optional[str] = None
    _remote: bool = field(default=True, init=False, repr=False)


def current_machine() -> str:
    """Return the hostname of the current machine."""
    return socket.gethostname()


class SessionSyncClient:
    """Async client for reading and pulling sessions from a GitHub repository."""

    def __init__(self, repo: str, token: Optional[str] = None) -> None:
        self._repo = repo
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    async def fetch_index(self) -> dict:
        """Fetch and decode sessions/index.json from the remote repo.

        Returns:
            Parsed dict mapping session UUID → metadata dict.

        Raises:
            httpx.HTTPStatusError: If the API request fails.
        """
        url = f"{_GH_API}/repos/{self._repo}/contents/sessions/index.json"
        async with httpx.AsyncClient(headers=self._headers, timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            if "content" not in data:
                if "download_url" in data:
                    raise ValueError(
                        "GitHub API returned download_url instead of "
                        "inline content for sessions/index.json"
                    )
                raise ValueError(
                    "GitHub API response missing 'content' key for sessions/index.json"
                )
            content = base64.b64decode(data["content"]).decode()
            return json.loads(content)

    async def list_remote(
        self, cwd_filter: Optional[str] = None
    ) -> list[RemoteSession]:
        """List remote sessions, optionally filtered by project name.

        The index uses a ``repository`` field (e.g. ``owner/repo``).  When
        *cwd_filter* is a local path, matching is done on the final path
        component against the repository name after the ``/``.

        Args:
            cwd_filter: Local working directory path used to derive the
                project/repo name for filtering.  Pass ``None`` to return all.

        Returns:
            List of RemoteSession objects sorted by updated_at descending.
        """
        try:
            index = await self.fetch_index()
        except Exception as e:
            logger.warning("Failed to fetch remote session index: %s", e)
            return []

        filter_name: Optional[str] = None
        if cwd_filter:
            filter_name = Path(cwd_filter).name.lower()

        sessions: list[RemoteSession] = []
        for session_id, entry in index.items():
            if not isinstance(entry, dict):
                continue
            repository: str = entry.get("repository") or ""
            repo_name = repository.split("/")[-1].lower() if repository else ""
            if filter_name and repo_name != filter_name:
                continue
            sessions.append(
                RemoteSession(
                    sessionId=session_id,
                    summary=entry.get("summary") or None,
                    startTime=entry.get("created_at"),
                    modifiedTime=entry.get("updated_at"),
                    cwd=repository,
                    machine=entry.get("machine") or None,
                )
            )

        return sorted(
            sessions,
            key=lambda s: s.modifiedTime or "",
            reverse=True,
        )

    async def pull_session(self, session_id: str) -> Path:
        """Pull session files from GitHub to the local session-state directory.

        Downloads events.jsonl and workspace.yaml (skips missing files).

        Args:
            session_id: The session UUID to pull.

        Returns:
            Path to the local session directory.

        Raises:
            ValueError: If session_id is not a valid UUID or escapes the
                session-state directory.
            httpx.HTTPStatusError: On non-404 API failures.
        """
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"Invalid session_id: {session_id!r}")
        base = (Path.home() / ".copilot" / "session-state").resolve()
        session_dir = (base / session_id).resolve()
        if not str(session_dir).startswith(str(base) + "/"):
            raise ValueError("session_id escapes session-state directory")
        session_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(headers=self._headers, timeout=15.0) as client:
            for filename in ("events.jsonl", "workspace.yaml"):
                url = (
                    f"{_GH_API}/repos/{self._repo}/contents/"
                    f"sessions/{session_id}/{filename}"
                )
                resp = await client.get(url)
                if resp.status_code == 404:
                    logger.debug(
                        "Remote session %s: %s not found, skipping",
                        session_id,
                        filename,
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "content" not in data:
                    if "download_url" in data:
                        raise ValueError(
                            f"GitHub API returned download_url instead "
                            f"of inline content for {filename}"
                        )
                    raise ValueError(
                        f"GitHub API response missing 'content' key for {filename}"
                    )
                raw = base64.b64decode(data["content"])
                (session_dir / filename).write_bytes(raw)
                logger.info("Pulled %s for session %s", filename, session_id)

        return session_dir


def get_sync_client() -> Optional["SessionSyncClient"]:
    """Return a configured SessionSyncClient, or None if not configured.

    Uses GITHUB_TOKEN env var if set, otherwise falls back to ``gh auth token``.
    The client is cached at module level after first creation.

    Returns:
        SessionSyncClient if COPILOT_SESSIONS_REPO is set and a token is
        available, else None.
    """
    global _cached_sync_client
    if not COPILOT_SESSIONS_REPO:
        return None
    if _cached_sync_client is not None:
        return _cached_sync_client
    token = GITHUB_TOKEN or _gh_cli_token()
    _cached_sync_client = SessionSyncClient(repo=COPILOT_SESSIONS_REPO, token=token)
    return _cached_sync_client


def _gh_cli_token() -> Optional[str]:
    """Retrieve a GitHub token from the ``gh`` CLI.

    Returns:
        Token string, or None if unavailable.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        token = result.stdout.strip()
        return token if token else None
    except Exception as e:
        logger.debug("gh auth token failed: %s", e)
        return None
