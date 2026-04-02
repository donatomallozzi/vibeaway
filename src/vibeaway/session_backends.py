from __future__ import annotations

import abc
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TITLE_MAX_LEN = 60
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _truncate_text(text: str, max_len: int = TITLE_MAX_LEN) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + "…" if len(text) > max_len else text


def _parse_iso_datetime(value: str, fallback: datetime | None = None) -> datetime | None:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _iter_jsonl(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.debug("Error reading %s: %s", path, exc)


def _format_session_list(
    agent_label: str,
    sessions: list["Session"],
    current_session_id: str | None = None,
    *,
    empty_message: str,
) -> str:
    if not sessions:
        return empty_message

    open_count = sum(1 for session in sessions if session.is_open)
    header = f"📋 {agent_label} sessions (most recent first)"
    if open_count:
        header += f" — {open_count} open"
    lines = [header + "\n"]

    for index, session in enumerate(sessions, 1):
        bot_marker = " ◀ bot" if session.session_id == current_session_id else ""
        project_short = Path(session.project_dir).name or session.project_dir or "(unknown)"
        open_badge = ""
        if session.is_open:
            if session.open_certainty == "exact":
                open_badge = f"  🟢 open (PID {session.open_pid})"
            else:
                open_badge = f"  🟡 probably open (PID {session.open_pid})"

        lines.append(
            f"#{index} - {session.short_id} - {session.age_str}{open_badge}{bot_marker}\n"
            f"   📁 {project_short}\n   💬 {session.title}\n"
        )

    lines.append("\nResume: /resume <#> or /resume <id> or /resume <title>")

    if any(session.open_certainty == "inferred" for session in sessions):
        lines.append(
            "\n🟡 = interactive process detected without explicit session id "
            "(probably open, not guaranteed)"
        )

    return "\n".join(lines)


def _extract_codex_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return "\n\n".join(texts).strip()


def _normalize_basic_usage(tokens: dict, session_id: str = "") -> dict:
    if not tokens and not session_id:
        return {}
    return {
        "usage": {
            "input_tokens": tokens.get("input_tokens", 0),
            "output_tokens": tokens.get("output_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": tokens.get("cached_input_tokens", 0),
        },
        "total_cost_usd": 0,
        "num_turns": 0,
        "duration_ms": 0,
        "duration_api_ms": 0,
        "session_id": session_id,
        "model_usage": {},
    }


@dataclass
class ActiveProcess:
    pid: int
    resume_id: str | None
    is_interactive: bool


@dataclass
class Session:
    session_id: str
    title: str
    project_dir: str
    last_modified: datetime
    message_count: int = 0
    raw_path: Path | None = field(repr=False, default=None)
    is_open: bool = False
    open_pid: int | None = None
    open_certainty: str = ""

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    @property
    def age_str(self) -> str:
        delta = datetime.now() - self.last_modified
        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = delta.seconds // 60
        return f"{minutes}m ago"

    @property
    def open_badge(self) -> str:
        if not self.is_open:
            return ""
        if self.open_certainty == "exact":
            return f"  🟢 *open* (PID {self.open_pid})"
        return f"  🟡 *probably open* (PID {self.open_pid})"


@dataclass
class SessionState:
    prompt: str = ""
    response: str = ""
    usage: dict = field(default_factory=dict)


class SessionBackend(abc.ABC):
    name = "session-backend"
    display_name = "Session backend"

    @abc.abstractmethod
    def list_sessions(self, workdir: str | None = None, limit: int = 20) -> list[Session]:
        raise NotImplementedError

    @abc.abstractmethod
    def format_session_list(
        self,
        sessions: list[Session],
        current_session_id: str | None = None,
    ) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def load_session_state(self, session_id: str, workdir: str) -> SessionState | None:
        raise NotImplementedError

    @abc.abstractmethod
    def read_last_interaction(self, workdir: str, session_id: str = "") -> tuple[str, str]:
        raise NotImplementedError


class ClaudeSessionBackend(SessionBackend):
    name = "claude"
    display_name = "Claude Code"

    def __init__(self) -> None:
        self._decode_cache: dict[str, str | None] = {}

    @property
    def sessions_dir(self) -> Path:
        return Path.home() / ".claude" / "projects"

    def encode_project_path(self, path: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "-", path)

    def is_uuid(self, value: str) -> bool:
        return bool(SESSION_ID_RE.match(value))

    def truncate(self, text: str, max_len: int = TITLE_MAX_LEN) -> str:
        return _truncate_text(text, max_len=max_len)

    def project_dir_matches(self, encoded_dir_name: str, workdir: str) -> bool:
        encoded_workdir = self.encode_project_path(workdir)
        return (
            encoded_dir_name.lower() == encoded_workdir.lower()
            or encoded_dir_name.lower().startswith(encoded_workdir.lower() + "-")
            or encoded_workdir.lower().startswith(encoded_dir_name.lower() + "-")
        )

    def get_active_processes(self) -> list[ActiveProcess]:
        if sys.platform.startswith("linux"):
            return self._get_processes_linux()
        return self._get_processes_ps()

    def _parse_cmdline(self, args: list[str]) -> ActiveProcess | None:
        if not args:
            return None
        binary = Path(args[0]).name
        if "claude" not in binary:
            return None
        if "--print" in args or "-p" in args:
            return None

        resume_id = None
        for i, arg in enumerate(args):
            if arg == "--resume" and i + 1 < len(args):
                candidate = args[i + 1]
                if self.is_uuid(candidate):
                    resume_id = candidate
                break

        return ActiveProcess(pid=0, resume_id=resume_id, is_interactive=True)

    def _get_processes_linux(self) -> list[ActiveProcess]:
        result = []
        proc_dir = Path("/proc")
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline_raw = (entry / "cmdline").read_bytes()
                args = [a.decode(errors="replace") for a in cmdline_raw.split(b"\x00") if a]
                ap = self._parse_cmdline(args)
                if ap:
                    ap.pid = int(entry.name)
                    result.append(ap)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
        return result

    def _get_processes_ps(self) -> list[ActiveProcess]:
        result = []
        try:
            out = subprocess.check_output(["ps", "-eo", "pid,args"], text=True, timeout=5)
            for line in out.splitlines()[1:]:
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid_str, cmdline = parts
                args = cmdline.split()
                ap = self._parse_cmdline(args)
                if ap:
                    ap.pid = int(pid_str)
                    result.append(ap)
        except Exception as exc:
            logger.debug("get_processes_ps failed: %s", exc)
        return result

    def _decode_project_dir(self, encoded_name: str) -> str | None:
        key = encoded_name.lower()
        if key in self._decode_cache:
            return self._decode_cache[key]
        result = self._try_decode(encoded_name)
        self._decode_cache[key] = result
        return result

    def _try_decode(self, encoded_name: str) -> str | None:
        enc_lower = encoded_name.lower()
        if len(enc_lower) < 2 or enc_lower[1] != "-":
            return None
        drive = encoded_name[0].upper() + ":\\"
        remainder = encoded_name[2:]
        if not remainder.startswith("-"):
            return None
        remainder = remainder[1:]

        current = Path(drive)
        while remainder:
            if not current.is_dir():
                return None
            best_match = None
            best_len = 0
            try:
                for child in current.iterdir():
                    if not child.is_dir():
                        continue
                    child_enc = self.encode_project_path(child.name).lower()
                    if remainder.lower() == child_enc:
                        return str(child)
                    if remainder.lower().startswith(child_enc + "-") and len(child_enc) > best_len:
                        best_match = child
                        best_len = len(child_enc)
            except PermissionError:
                return None

            if best_match is None:
                return None
            current = best_match
            remainder = remainder[best_len + 1 :]

        if current.is_dir() and self.encode_project_path(str(current)).lower() == enc_lower:
            return str(current)
        return None

    def _extract_title(self, jsonl_path: Path) -> tuple[str, int]:
        title = "(untitled)"
        count = 0
        for entry in _iter_jsonl(jsonl_path):
            count += 1
            if count > 50 and title != "(untitled)":
                break

            if title != "(untitled)":
                continue

            if entry.get("type") in ("human", "user"):
                msg = entry.get("message", {})
                content = msg.get("content", "")
            elif entry.get("role") == "user":
                content = entry.get("content", "")
            else:
                continue

            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        content = block.get("text", "")
                        break
            if isinstance(content, str) and content.strip():
                title = self.truncate(content.strip())

        return title, count

    def _extract_message_text(self, msg: dict) -> str:
        content = msg.get("content", "")
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(texts).strip()
        return content.strip() if isinstance(content, str) else ""

    def _read_last_from_jsonl(self, jsonl_file: Path) -> tuple[str, str]:
        last_prompt = ""
        last_response = ""
        for entry in _iter_jsonl(jsonl_file):
            etype = entry.get("type")
            msg = entry.get("message", {})
            role = msg.get("role", "")

            if etype == "user" or role == "user":
                text = self._extract_message_text(msg)
                if text:
                    last_prompt = text
            elif etype == "assistant" or role == "assistant":
                text = self._extract_message_text(msg)
                if text:
                    last_response = text

        return last_prompt, last_response

    def _extract_usage_dict(self, entry: dict, fallback_session_id: str = "") -> dict:
        etype = entry.get("type")
        if etype == "result":
            return {
                "usage": entry.get("usage", {}),
                "total_cost_usd": entry.get("total_cost_usd", 0),
                "num_turns": entry.get("num_turns", 0),
                "duration_ms": entry.get("duration_ms", 0),
                "duration_api_ms": entry.get("duration_api_ms", 0),
                "session_id": entry.get("session_id", fallback_session_id),
                "model_usage": entry.get("modelUsage", {}),
            }
        msg = entry.get("message", {})
        usage = msg.get("usage")
        if usage and (etype == "assistant" or msg.get("role") == "assistant"):
            return {
                "usage": usage,
                "total_cost_usd": 0,
                "num_turns": 0,
                "duration_ms": 0,
                "duration_api_ms": 0,
                "session_id": entry.get("sessionId", fallback_session_id),
                "model_usage": {},
            }
        return {}

    def find_session_file(self, workdir: str, session_id: str = "") -> Path | None:
        encoded = self.encode_project_path(workdir)
        project_dir = None
        if self.sessions_dir.is_dir():
            for directory in self.sessions_dir.iterdir():
                if directory.is_dir() and directory.name.lower() == encoded.lower():
                    project_dir = directory
                    break

        if not project_dir:
            return None

        if session_id:
            for file in project_dir.glob("*.jsonl"):
                if file.stem.startswith(session_id):
                    return file

        jsonls = sorted(project_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        return jsonls[0] if jsonls else None

    def read_last_interaction(self, workdir: str, session_id: str = "") -> tuple[str, str]:
        jsonl = self.find_session_file(workdir, session_id=session_id)
        if not jsonl:
            return "", ""
        return self._read_last_from_jsonl(jsonl)

    def load_session_state(self, session_id: str, workdir: str) -> SessionState | None:
        jsonl = self.find_session_file(workdir, session_id=session_id)
        if not jsonl:
            return None

        prompt, response = self._read_last_from_jsonl(jsonl)
        usage: dict = {}
        for entry in _iter_jsonl(jsonl):
            usage_dict = self._extract_usage_dict(entry, session_id)
            if usage_dict:
                usage = usage_dict

        return SessionState(prompt=prompt, response=response, usage=usage)

    def list_sessions(self, workdir: str | None = None, limit: int = 20) -> list[Session]:
        if not self.sessions_dir.exists():
            logger.warning("Session directory not found: %s", self.sessions_dir)
            return []

        sessions: list[Session] = []
        for project_dir in self.sessions_dir.iterdir():
            if not project_dir.is_dir():
                continue
            if workdir and not self.project_dir_matches(project_dir.name, workdir):
                continue

            for jsonl_file in project_dir.glob("*.jsonl"):
                session_id = jsonl_file.stem
                if not self.is_uuid(session_id):
                    continue
                try:
                    mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime)
                except OSError:
                    continue

                title, count = self._extract_title(jsonl_file)
                real_dir = self._decode_project_dir(project_dir.name) or workdir or project_dir.name
                sessions.append(
                    Session(
                        session_id=session_id,
                        title=title,
                        project_dir=real_dir,
                        last_modified=mtime,
                        message_count=count,
                        raw_path=jsonl_file,
                    )
                )

        sessions.sort(key=lambda session: session.last_modified, reverse=True)
        sessions = sessions[:limit]

        try:
            active_procs = self.get_active_processes()
        except Exception as exc:
            logger.warning("Process detection failed: %s", exc)
            active_procs = []

        if active_procs:
            by_id = {session.session_id: session for session in sessions}
            most_recent = sessions[0] if sessions else None
            for ap in active_procs:
                if ap.resume_id and ap.resume_id in by_id:
                    session = by_id[ap.resume_id]
                    session.is_open = True
                    session.open_pid = ap.pid
                    session.open_certainty = "exact"
                elif most_recent and not most_recent.is_open:
                    most_recent.is_open = True
                    most_recent.open_pid = ap.pid
                    most_recent.open_certainty = "inferred"

        return sessions

    def format_session_list(
        self,
        sessions: list[Session],
        current_session_id: str | None = None,
    ) -> str:
        return _format_session_list(
            self.display_name,
            sessions,
            current_session_id=current_session_id,
            empty_message="📭 No sessions found.\n\nStart a conversation with Claude Code to create one.",
        )


class CodexSessionBackend(SessionBackend):
    name = "codex"
    display_name = "Codex CLI"

    @property
    def sessions_dir(self) -> Path:
        return Path.home() / ".codex" / "sessions"

    @property
    def session_index(self) -> Path:
        return Path.home() / ".codex" / "session_index.jsonl"

    def is_uuid(self, value: str) -> bool:
        return bool(SESSION_ID_RE.match(value))

    def truncate(self, text: str, max_len: int = TITLE_MAX_LEN) -> str:
        return _truncate_text(text, max_len=max_len)

    def _read_index(self) -> dict[str, dict]:
        index: dict[str, dict] = {}
        for entry in _iter_jsonl(self.session_index):
            session_id = entry.get("id", "")
            if not self.is_uuid(session_id):
                continue
            index[session_id] = entry
        return index

    def _scan_session_file(self, jsonl_path: Path) -> dict:
        session_id = ""
        cwd = ""
        title = "(untitled)"
        last_prompt = ""
        last_response = ""
        usage: dict = {}
        message_count = 0

        for entry in _iter_jsonl(jsonl_path):
            etype = entry.get("type")
            payload = entry.get("payload", {})

            if etype == "session_meta":
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd", cwd)
                continue

            if etype == "response_item" and payload.get("type") == "message":
                role = payload.get("role", "")
                text = _extract_codex_text(payload.get("content", []))
                if not text:
                    continue
                if role == "user":
                    last_prompt = text
                    message_count += 1
                    if title == "(untitled)":
                        title = self.truncate(text)
                elif role == "assistant":
                    last_response = text
                    message_count += 1
                continue

            if etype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = _normalize_basic_usage(info.get("last_token_usage") or {}, session_id=session_id)

        return {
            "session_id": session_id,
            "cwd": cwd,
            "title": title,
            "last_prompt": last_prompt,
            "last_response": last_response,
            "usage": usage,
            "message_count": message_count,
        }

    def _iter_session_files(self):
        if not self.sessions_dir.exists():
            return []
        return self.sessions_dir.rglob("*.jsonl")

    def _find_file_by_session_id(self, session_id: str) -> Path | None:
        if not session_id:
            return None
        for path in self._iter_session_files():
            if session_id in path.stem:
                return path
        return None

    def list_sessions(self, workdir: str | None = None, limit: int = 20) -> list[Session]:
        if not self.sessions_dir.exists():
            logger.warning("Session directory not found: %s", self.sessions_dir)
            return []

        index = self._read_index()
        sessions: list[Session] = []
        normalized_workdir = Path(workdir).resolve() if workdir else None

        for jsonl_file in self._iter_session_files():
            scan = self._scan_session_file(jsonl_file)
            session_id = scan["session_id"]
            if not self.is_uuid(session_id):
                continue

            cwd = scan["cwd"]
            if normalized_workdir and cwd:
                try:
                    if Path(cwd).resolve() != normalized_workdir:
                        continue
                except OSError:
                    continue

            try:
                mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime)
            except OSError:
                continue

            idx_entry = index.get(session_id, {})
            title = idx_entry.get("thread_name") or scan["title"] or "(untitled)"
            updated_at = _parse_iso_datetime(idx_entry.get("updated_at", ""), fallback=mtime) or mtime
            project_dir = cwd or "(unknown)"

            sessions.append(
                Session(
                    session_id=session_id,
                    title=self.truncate(title),
                    project_dir=project_dir,
                    last_modified=updated_at,
                    message_count=scan["message_count"],
                    raw_path=jsonl_file,
                )
            )

        sessions.sort(key=lambda session: session.last_modified, reverse=True)
        return sessions[:limit]

    def format_session_list(
        self,
        sessions: list[Session],
        current_session_id: str | None = None,
    ) -> str:
        return _format_session_list(
            self.display_name,
            sessions,
            current_session_id=current_session_id,
            empty_message="📭 No sessions found.\n\nStart a Codex CLI conversation to create one.",
        )

    def load_session_state(self, session_id: str, workdir: str) -> SessionState | None:
        jsonl = self._find_file_by_session_id(session_id)
        if not jsonl and session_id:
            return None
        if not jsonl:
            candidates = self.list_sessions(workdir=workdir, limit=1)
            jsonl = candidates[0].raw_path if candidates else None
        if not jsonl:
            return None

        scan = self._scan_session_file(jsonl)
        return SessionState(
            prompt=scan["last_prompt"],
            response=scan["last_response"],
            usage=scan["usage"],
        )

    def read_last_interaction(self, workdir: str, session_id: str = "") -> tuple[str, str]:
        if session_id:
            state = self.load_session_state(session_id, workdir)
            if state is not None:
                return state.prompt, state.response
            return "", ""

        sessions = self.list_sessions(workdir=workdir, limit=1)
        if not sessions or not sessions[0].raw_path:
            return "", ""
        scan = self._scan_session_file(sessions[0].raw_path)
        return scan["last_prompt"], scan["last_response"]


class CopilotSessionBackend(SessionBackend):
    name = "copilot"
    display_name = "GitHub Copilot CLI"

    @property
    def sessions_dir(self) -> Path:
        return Path.home() / ".copilot" / "session-state"

    def is_uuid(self, value: str) -> bool:
        return bool(SESSION_ID_RE.match(value))

    def truncate(self, text: str, max_len: int = TITLE_MAX_LEN) -> str:
        return _truncate_text(text, max_len=max_len)

    def _scan_session_file(self, jsonl_path: Path) -> dict:
        session_id = jsonl_path.stem
        title = "(untitled)"
        last_prompt = ""
        last_response = ""
        message_count = 0

        for entry in _iter_jsonl(jsonl_path):
            etype = entry.get("type")
            data = entry.get("data", {})

            if etype == "session.start":
                session_id = data.get("sessionId", session_id)
                continue

            if etype == "user.message":
                text = data.get("content", "")
                if isinstance(text, str) and text.strip():
                    last_prompt = text.strip()
                    message_count += 1
                    if title == "(untitled)":
                        title = self.truncate(last_prompt)
                continue

            if etype in {"assistant.message", "assistant.response"}:
                text = data.get("content", "")
                if isinstance(text, str) and text.strip():
                    last_response = text.strip()
                    message_count += 1
                continue

            if etype == "session.error":
                text = data.get("message", "")
                if isinstance(text, str) and text.strip():
                    last_response = text.strip()

        return {
            "session_id": session_id,
            "title": title,
            "last_prompt": last_prompt,
            "last_response": last_response,
            "message_count": message_count,
        }

    def _iter_session_files(self):
        if not self.sessions_dir.exists():
            return []
        return self.sessions_dir.glob("*.jsonl")

    def _find_file_by_session_id(self, session_id: str) -> Path | None:
        if not session_id:
            return None
        for path in self._iter_session_files():
            if path.stem.startswith(session_id):
                return path
        return None

    def list_sessions(self, workdir: str | None = None, limit: int = 20) -> list[Session]:
        if not self.sessions_dir.exists():
            logger.warning("Session directory not found: %s", self.sessions_dir)
            return []

        sessions: list[Session] = []
        for jsonl_file in self._iter_session_files():
            scan = self._scan_session_file(jsonl_file)
            session_id = scan["session_id"]
            if not self.is_uuid(session_id):
                continue

            try:
                mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime)
            except OSError:
                continue

            sessions.append(
                Session(
                    session_id=session_id,
                    title=scan["title"],
                    project_dir="(unknown)",
                    last_modified=mtime,
                    message_count=scan["message_count"],
                    raw_path=jsonl_file,
                )
            )

        sessions.sort(key=lambda session: session.last_modified, reverse=True)
        return sessions[:limit]

    def format_session_list(
        self,
        sessions: list[Session],
        current_session_id: str | None = None,
    ) -> str:
        return _format_session_list(
            self.display_name,
            sessions,
            current_session_id=current_session_id,
            empty_message="📭 No sessions found.\n\nStart a GitHub Copilot CLI conversation to create one.",
        )

    def load_session_state(self, session_id: str, workdir: str) -> SessionState | None:
        jsonl = self._find_file_by_session_id(session_id)
        if not jsonl and session_id:
            return None
        if not jsonl:
            sessions = self.list_sessions(limit=1)
            jsonl = sessions[0].raw_path if sessions else None
        if not jsonl:
            return None

        scan = self._scan_session_file(jsonl)
        return SessionState(
            prompt=scan["last_prompt"],
            response=scan["last_response"],
            usage={},
        )

    def read_last_interaction(self, workdir: str, session_id: str = "") -> tuple[str, str]:
        if session_id:
            state = self.load_session_state(session_id, workdir)
            if state is not None:
                return state.prompt, state.response
            return "", ""

        sessions = self.list_sessions(limit=1)
        if not sessions or not sessions[0].raw_path:
            return "", ""
        scan = self._scan_session_file(sessions[0].raw_path)
        return scan["last_prompt"], scan["last_response"]


_SESSION_BACKENDS: dict[str, SessionBackend] = {
    "claude": ClaudeSessionBackend(),
    "codex": CodexSessionBackend(),
    "copilot": CopilotSessionBackend(),
}


def get_session_backend(name: str = "claude") -> SessionBackend:
    key = name.lower()
    try:
        return _SESSION_BACKENDS[key]
    except KeyError as exc:
        available = ", ".join(sorted(_SESSION_BACKENDS))
        raise KeyError(f"Unknown session backend '{name}'. Available: {available}") from exc


def available_session_backends() -> tuple[str, ...]:
    return tuple(sorted(_SESSION_BACKENDS))
