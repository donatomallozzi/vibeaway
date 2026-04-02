"""Backward-compatible session helpers backed by pluggable session backends.

Today this module still exposes Claude Code session behavior, but the actual
implementation now lives behind a backend object so other CLI agents can add
their own session providers later.
"""

from __future__ import annotations

import logging
import re
from typing import cast

from vibeaway.session_backends import (
    ActiveProcess,
    ClaudeSessionBackend,
    Session,
    SessionState,
    get_session_backend,
)

logger = logging.getLogger(__name__)

_CLAUDE_BACKEND = cast(ClaudeSessionBackend, get_session_backend("claude"))


def _backend(name: str = "claude"):
    return get_session_backend(name)


def get_active_processes() -> list[ActiveProcess]:
    return _CLAUDE_BACKEND.get_active_processes()


def _encode_project_path(path: str) -> str:
    return _CLAUDE_BACKEND.encode_project_path(path)


def _project_dir_matches(encoded_dir_name: str, workdir: str) -> bool:
    return _CLAUDE_BACKEND.project_dir_matches(encoded_dir_name, workdir)


def _truncate(text: str, max_len: int = 60) -> str:
    return _CLAUDE_BACKEND.truncate(text, max_len=max_len)


def _is_uuid(value: str) -> bool:
    return _CLAUDE_BACKEND.is_uuid(value)


def list_sessions(workdir: str | None = None, limit: int = 20, backend: str = "claude") -> list[Session]:
    return _backend(backend).list_sessions(workdir=workdir, limit=limit)


def find_session(query: str, workdir: str | None = None, backend: str = "claude") -> Session | None:
    """Search a session by ordinal number, UUID/prefix, or title substring."""
    sessions = list_sessions(workdir=workdir, limit=200, backend=backend)
    query_lower = query.lower().strip()
    logger.info("Session lookup: query=%s among %d sessions", query, len(sessions))

    if query_lower.isdigit():
        idx = int(query_lower) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]

    for session in sessions:
        if session.session_id == query_lower:
            return session

    if len(query_lower) >= 4 and re.match(r"^[0-9a-f-]+$", query_lower):
        for session in sessions:
            if session.session_id.startswith(query_lower):
                return session

    for session in sessions:
        if query_lower in session.title.lower():
            return session

    return None


def format_session_list(
    sessions: list[Session],
    current_session_id: str | None = None,
    backend: str = "claude",
) -> str:
    return _backend(backend).format_session_list(sessions, current_session_id=current_session_id)


def load_session_state(session_id: str, workdir: str, backend: str = "claude") -> SessionState | None:
    return _backend(backend).load_session_state(session_id, workdir)


def read_last_interaction(
    workdir: str,
    session_id: str = "",
    backend: str = "claude",
) -> tuple[str, str]:
    return _backend(backend).read_last_interaction(workdir, session_id=session_id)
