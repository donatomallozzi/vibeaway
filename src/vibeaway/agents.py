from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from vibeaway import config
from vibeaway.locales import t

logger = logging.getLogger(__name__)


def _extract_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict):
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


def _mk_output_path(prefix: str) -> Path:
    fd, path = tempfile.mkstemp(prefix=f"{prefix}-", suffix=".txt")
    os.close(fd)
    return Path(path)


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _latest_matching_dir(root: Path, pattern: str) -> Path | None:
    try:
        matches = [p for p in root.glob(pattern) if p.is_dir()]
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _resolve_command(
    default_name: str,
    *,
    env: dict[str, str],
    env_vars: tuple[str, ...] = (),
    windows_candidates: Callable[[], list[Path]] | None = None,
) -> str:
    for key in env_vars:
        value = (env.get(key) or "").strip()
        if not value:
            continue
        resolved = shutil.which(value, path=env.get("PATH"))
        if resolved:
            return resolved
        candidate = Path(value).expanduser()
        if candidate.exists():
            return str(candidate)
    resolved = shutil.which(default_name, path=env.get("PATH"))
    if resolved:
        return resolved
    if os.name == "nt" and windows_candidates is not None:
        for candidate in windows_candidates():
            try:
                if candidate.exists():
                    return str(candidate)
            except OSError:
                continue
    return default_name


def _spawn_to_files(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.Popen:
    with stdout_path.open("w", encoding="utf-8", errors="replace") as out_fp:
        with stderr_path.open("w", encoding="utf-8", errors="replace") as err_fp:
            return subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_fp,
                stderr=err_fp,
                text=True,
            )


def _spawn_to_pipes(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
) -> subprocess.Popen:
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def _pump_text_stream(
    stream,
    sink: "queue.Queue[tuple[str, str | None]]",
    source: str,
) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.put((source, line))
    finally:
        try:
            stream.close()
        except Exception:
            pass
        sink.put((source, None))


async def _run_windows_file_process(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    prefix: str,
    timeout: int,
    preview_path: Path | None = None,
    on_update: Callable[[str], Awaitable[None]] | None = None,
    stream_interval: float = 2.0,
    cancel_event: asyncio.Event | None = None,
    on_process: Callable[[object], None] | None = None,
) -> tuple[str, str, str, bool]:
    stdout_path = _mk_output_path(f"{prefix}-stdout")
    stderr_path = _mk_output_path(f"{prefix}-stderr")
    try:
        proc = _spawn_to_files(cmd, cwd=cwd, env=env, stdout_path=stdout_path, stderr_path=stderr_path)
        if on_process is not None:
            on_process(proc)
        deadline = asyncio.get_running_loop().time() + timeout
        last_update = 0.0
        last_preview = ""
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                raise asyncio.CancelledError
            if asyncio.get_running_loop().time() > deadline:
                proc.kill()
                return last_preview, _read_text_file(stdout_path), _read_text_file(stderr_path), False
            current = _read_text_file(preview_path or stdout_path)
            now = asyncio.get_running_loop().time()
            if on_update and current and current != last_preview and (now - last_update) >= stream_interval:
                last_preview = current
                try:
                    await on_update(current)
                except Exception:
                    pass
                last_update = now
            await asyncio.sleep(0.25)
        preview = _read_text_file(preview_path or stdout_path)
        return preview, _read_text_file(stdout_path), _read_text_file(stderr_path), True
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class AgentCapabilities:
    sessions: bool = False
    usage: bool = False
    streaming: bool = True
    permission_modes: tuple[str, ...] = ()


@dataclass
class AgentRunResult:
    text: str
    usage: dict = field(default_factory=dict)


class AgentAdapter(abc.ABC):
    name = "agent"
    display_name = "Agent"
    capabilities = AgentCapabilities()

    def subprocess_env(self) -> dict[str, str]:
        env = {**os.environ, "TERM": "dumb"}
        extra = os.environ.get("EXTRA_PATH", "")
        if extra:
            env["PATH"] = extra + os.pathsep + env.get("PATH", "")
        return env

    def normalize_permission_mode(self, permission_mode: str = "default") -> str:
        allowed = self.capabilities.permission_modes
        if permission_mode in allowed:
            return permission_mode
        if "default" in allowed:
            return "default"
        return allowed[0] if allowed else permission_mode

    @abc.abstractmethod
    def resolve_executable(self) -> str: ...

    @abc.abstractmethod
    def build_cmd(self, prompt: str, continue_session: bool, resume_id: str | None, *, streaming: bool = False, permission_mode: str = "default") -> list[str]: ...

    @abc.abstractmethod
    async def run_batch(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, timeout: int | None = None, permission_mode: str = "default", on_process: Callable[[object], None] | None = None) -> AgentRunResult: ...

    @abc.abstractmethod
    async def run_stream(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, on_update: Callable[[str], Awaitable[None]] | None = None, stream_interval: float = 2.0, timeout: int | None = None, permission_mode: str = "default", cancel_event: asyncio.Event | None = None, on_process: Callable[[object], None] | None = None) -> AgentRunResult: ...


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude"
    display_name = "Claude Code"
    capabilities = AgentCapabilities(True, True, True, ("default", "acceptEdits", "bypassPermissions"))
    _SYSTEM_PROMPT_APPEND = "You are running inside a Telegram bot bridge. The user interacts with you via text messages and voice messages on Telegram. When they send a voice message, it is automatically transcribed to text (via Whisper), sent to you, and your text response is automatically converted to speech (via OpenAI TTS) and sent back as a voice message. You DO have voice/audio capabilities through this bridge. If the user asks about your voice or audio capabilities, confirm that you can receive and respond with voice messages through the Telegram bot."

    @staticmethod
    def _windows_candidates() -> list[Path]:
        appdata = Path(os.environ.get("APPDATA", ""))
        return [appdata / "npm" / "claude.cmd", appdata / "npm" / "claude"]

    def resolve_executable(self) -> str:
        return _resolve_command("claude", env=self.subprocess_env(), env_vars=("CLAUDE_CLI", "CLAUDE_PATH"), windows_candidates=self._windows_candidates)

    def build_cmd(self, prompt: str, continue_session: bool, resume_id: str | None, *, streaming: bool = False, permission_mode: str = "bypassPermissions") -> list[str]:
        cmd = [self.resolve_executable(), "--print", prompt]
        if streaming:
            cmd += ["--verbose", "--output-format", "stream-json"]
        mode = self.normalize_permission_mode(permission_mode)
        if mode != "default":
            cmd += ["--permission-mode", mode]
        cmd += ["--append-system-prompt", self._SYSTEM_PROMPT_APPEND]
        if resume_id:
            cmd += ["--resume", resume_id]
        elif continue_session:
            cmd.append("--continue")
        return cmd

    def _cli_not_found_message(self) -> str:
        translated = t("cli_not_found")
        return translated if translated != "cli_not_found" else "❌ `claude` CLI not found.\nInstall Claude Code: `npm install -g @anthropic-ai/claude-code`"

    async def run_batch(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, timeout: int | None = None, permission_mode: str = "bypassPermissions", on_process: Callable[[object], None] | None = None) -> AgentRunResult:
        effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
        cmd = self.build_cmd(prompt, continue_session, resume_id, streaming=False, permission_mode=permission_mode)
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workdir, env=self.subprocess_env())
            if on_process:
                on_process(proc)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return AgentRunResult(f"⏱ Timeout after {effective_timeout}s.")
            output = stdout.decode(errors="replace").strip()
            if not output and stderr:
                output = f"⚠️ stderr:\n{stderr.decode(errors='replace').strip()}"
            return AgentRunResult(output or "_(no output)_")
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            return AgentRunResult(self._cli_not_found_message())
        except Exception as exc:
            logger.exception("Unexpected error in Claude batch execution")
            return AgentRunResult(f"❌ Error: {exc}")

    async def run_stream(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, on_update: Callable[[str], Awaitable[None]] | None = None, stream_interval: float = 2.0, timeout: int | None = None, permission_mode: str = "bypassPermissions", cancel_event: asyncio.Event | None = None, on_process: Callable[[object], None] | None = None) -> AgentRunResult:
        effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
        cmd = self.build_cmd(prompt, continue_session, resume_id, streaming=True, permission_mode=permission_mode)
        accumulated = ""
        final_result = ""
        last_update = 0.0
        usage: dict = {}
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workdir, env=self.subprocess_env(), limit=1024 * 1024)
            if on_process:
                on_process(proc)
            deadline = asyncio.get_running_loop().time() + effective_timeout
            async for raw_line in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    raise asyncio.CancelledError
                if asyncio.get_running_loop().time() > deadline:
                    proc.kill()
                    return AgentRunResult(accumulated or f"⏱ Timeout after {effective_timeout}s.")
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "assistant":
                    accumulated = _extract_text_content(event.get("message", {}).get("content", [])) or accumulated
                    now = asyncio.get_running_loop().time()
                    if on_update and accumulated and (now - last_update) >= stream_interval:
                        try:
                            await on_update(accumulated)
                        except Exception:
                            pass
                        last_update = now
                elif event.get("type") == "result":
                    usage = {"usage": event.get("usage", {}), "total_cost_usd": event.get("total_cost_usd", 0), "num_turns": event.get("num_turns", 0), "duration_ms": event.get("duration_ms", 0), "duration_api_ms": event.get("duration_api_ms", 0), "session_id": event.get("session_id", ""), "model_usage": event.get("modelUsage", {})}
                    if event.get("subtype") == "success":
                        final_result = (event.get("result") or accumulated).strip()
                    else:
                        err = event.get("error") or event.get("result") or "unknown error"
                        return AgentRunResult(f"❌ Claude error: {err}", usage=usage)
            await proc.wait()
            result = final_result or accumulated
            if not result:
                stderr_raw = await proc.stderr.read()
                if stderr_raw:
                    result = f"⚠️ stderr:\n{stderr_raw.decode(errors='replace').strip()}"
            return AgentRunResult(result or "_(no output)_", usage=usage)
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            return AgentRunResult(self._cli_not_found_message())
        except Exception as exc:
            logger.exception("Unexpected error in Claude stream execution")
            return AgentRunResult(f"❌ Error: {exc}")


class CodexAdapter(AgentAdapter):
    name = "codex"
    display_name = "Codex CLI"
    capabilities = AgentCapabilities(True, True, True, ("default", "acceptEdits", "bypassPermissions"))

    @staticmethod
    def _windows_candidates() -> list[Path]:
        appdata = Path(os.environ.get("APPDATA", ""))
        candidates = [appdata / "npm" / "codex.cmd", appdata / "npm" / "codex"]
        ext = _latest_matching_dir(Path.home() / ".vscode" / "extensions", "openai.chatgpt-*")
        if ext is not None:
            candidates.append(ext / "bin" / "windows-x86_64" / "codex.exe")
        return candidates

    def resolve_executable(self) -> str:
        return _resolve_command("codex", env=self.subprocess_env(), env_vars=("CODEX_CLI", "CODEX_PATH"), windows_candidates=self._windows_candidates)

    def _cli_not_found_message(self) -> str:
        return "❌ `codex` CLI not found.\nInstall Codex CLI and make sure `codex` is available in `PATH`."

    def _build_usage(self, info: dict, session_id: str = "") -> dict:
        return _normalize_basic_usage(info.get("last_token_usage") or {}, session_id=session_id)

    def _parse_usage(self, stdout_text: str, session_id: str = "") -> dict:
        usage: dict = {}
        current_session = session_id
        for line in stdout_text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "session_meta":
                current_session = event.get("payload", {}).get("id", current_session)
            elif event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "token_count":
                usage = self._build_usage(event.get("payload", {}).get("info") or {}, current_session)
        return usage

    def _extract_stream_text(self, event: dict) -> str:
        etype = event.get("type")
        payload = event.get("payload", {})

        if etype == "event_msg" and payload.get("type") == "agent_message":
            message = payload.get("message")
            return message.strip() if isinstance(message, str) else ""

        if etype != "response_item":
            return ""

        if payload.get("type") == "message" and payload.get("role") == "assistant":
            return _extract_text_content(payload.get("content", []))

        if payload.get("type") != "reasoning":
            return ""

        summary = payload.get("summary", [])
        if not isinstance(summary, list):
            return ""

        texts: list[str] = []
        for item in summary:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        return "\n\n".join(texts).strip()

    def build_cmd(self, prompt: str, continue_session: bool, resume_id: str | None, *, streaming: bool = False, permission_mode: str = "default") -> list[str]:
        cmd = [self.resolve_executable(), "exec"]
        if resume_id or continue_session:
            cmd += ["resume", resume_id or "--last"]
        cmd.append("--skip-git-repo-check")
        if streaming:
            cmd.append("--json")
        mode = self.normalize_permission_mode(permission_mode)
        if mode == "acceptEdits":
            cmd.append("--full-auto")
        elif mode == "bypassPermissions":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        cmd.append(prompt)
        return cmd

    async def run_batch(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, timeout: int | None = None, permission_mode: str = "default", on_process: Callable[[object], None] | None = None) -> AgentRunResult:
        effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
        output_path = _mk_output_path(self.name)
        try:
            cmd = self.build_cmd(prompt, continue_session, resume_id, streaming=True, permission_mode=permission_mode)
            cmd = cmd[:-1] + ["-o", str(output_path), cmd[-1]]
            if os.name == "nt":
                preview, stdout_text, stderr_text, completed = await _run_windows_file_process(cmd, cwd=workdir, env=self.subprocess_env(), prefix=self.name, timeout=effective_timeout, preview_path=output_path, on_process=on_process)
                if not completed:
                    return AgentRunResult(f"⏱ Timeout after {effective_timeout}s.")
                output = preview or stdout_text.strip() or (f"⚠️ stderr:\n{stderr_text}" if stderr_text else "")
                return AgentRunResult(output or "_(no output)_", usage=self._parse_usage(stdout_text))
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workdir, env=self.subprocess_env())
            if on_process:
                on_process(proc)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return AgentRunResult(f"⏱ Timeout after {effective_timeout}s.")
            stdout_text = stdout.decode(errors="replace")
            output = _read_text_file(output_path) or stdout_text.strip() or (f"⚠️ stderr:\n{stderr.decode(errors='replace').strip()}" if stderr else "")
            return AgentRunResult(output or "_(no output)_", usage=self._parse_usage(stdout_text))
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            return AgentRunResult(self._cli_not_found_message())
        except Exception as exc:
            logger.exception("Unexpected error in Codex batch execution")
            return AgentRunResult(f"❌ Error: {exc}")
        finally:
            output_path.unlink(missing_ok=True)

    async def run_stream(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, on_update: Callable[[str], Awaitable[None]] | None = None, stream_interval: float = 2.0, timeout: int | None = None, permission_mode: str = "default", cancel_event: asyncio.Event | None = None, on_process: Callable[[object], None] | None = None) -> AgentRunResult:
        effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
        output_path = _mk_output_path(self.name)
        try:
            cmd = self.build_cmd(prompt, continue_session, resume_id, streaming=True, permission_mode=permission_mode)
            cmd = cmd[:-1] + ["-o", str(output_path), cmd[-1]]
            if os.name == "nt":
                proc = _spawn_to_pipes(cmd, cwd=workdir, env=self.subprocess_env())
                if on_process:
                    on_process(proc)
                event_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
                threading.Thread(target=_pump_text_stream, args=(proc.stdout, event_queue, "stdout"), daemon=True).start()
                threading.Thread(target=_pump_text_stream, args=(proc.stderr, event_queue, "stderr"), daemon=True).start()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + effective_timeout
                accumulated = ""
                usage: dict = {}
                session_id = resume_id or ""
                stdout_lines: list[str] = []
                stderr_lines: list[str] = []
                stdout_done = False
                stderr_done = False
                last_update = 0.0
                while proc.poll() is None or not (stdout_done and stderr_done and event_queue.empty()):
                    if cancel_event is not None and cancel_event.is_set():
                        proc.kill()
                        raise asyncio.CancelledError
                    now = loop.time()
                    if now > deadline:
                        proc.kill()
                        return AgentRunResult(accumulated or f"Timeout after {effective_timeout}s.")
                    processed = False
                    while True:
                        try:
                            source, raw_line = event_queue.get_nowait()
                        except queue.Empty:
                            break
                        processed = True
                        if raw_line is None:
                            if source == "stdout":
                                stdout_done = True
                            else:
                                stderr_done = True
                            continue
                        if source == "stderr":
                            stderr_lines.append(raw_line)
                            continue
                        stdout_lines.append(raw_line)
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event.get("payload", {})
                        if event.get("type") == "session_meta":
                            session_id = payload.get("id", session_id)
                        elif event.get("type") == "event_msg" and payload.get("type") == "token_count":
                            usage = self._build_usage(payload.get("info") or {}, session_id)
                        text = self._extract_stream_text(event)
                        if text:
                            accumulated = text
                            if on_update and (now - last_update) >= stream_interval:
                                try:
                                    await on_update(accumulated)
                                except Exception:
                                    pass
                                last_update = loop.time()
                    if not processed:
                        await asyncio.sleep(0.05)
                stdout_text = "".join(stdout_lines)
                stderr_text = "".join(stderr_lines).strip()
                output = _read_text_file(output_path) or accumulated or (f"stderr:\n{stderr_text}" if stderr_text else "")
                return AgentRunResult(output or "_(no output)_", usage=usage or self._parse_usage(stdout_text, session_id=session_id))
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workdir, env=self.subprocess_env(), limit=1024 * 1024)
            if on_process:
                on_process(proc)
            deadline = asyncio.get_running_loop().time() + effective_timeout
            accumulated = ""
            usage: dict = {}
            session_id = resume_id or ""
            last_update = 0.0
            async for raw_line in proc.stdout:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    raise asyncio.CancelledError
                if asyncio.get_running_loop().time() > deadline:
                    proc.kill()
                    return AgentRunResult(accumulated or f"⏱ Timeout after {effective_timeout}s.")
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload", {})
                if event.get("type") == "session_meta":
                    session_id = payload.get("id", session_id)
                elif event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    usage = self._build_usage(payload.get("info") or {}, session_id)
                text = self._extract_stream_text(event)
                if text:
                    accumulated = text
                    now = asyncio.get_running_loop().time()
                    if on_update and (now - last_update) >= stream_interval:
                        try:
                            await on_update(accumulated)
                        except Exception:
                            pass
                        last_update = now
            await proc.wait()
            output = _read_text_file(output_path) or accumulated
            if not output:
                stderr_raw = await proc.stderr.read()
                if stderr_raw:
                    output = f"⚠️ stderr:\n{stderr_raw.decode(errors='replace').strip()}"
            return AgentRunResult(output or "_(no output)_", usage=usage)
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            return AgentRunResult(self._cli_not_found_message())
        except Exception as exc:
            logger.exception("Unexpected error in Codex stream execution")
            return AgentRunResult(f"❌ Error: {exc}")
        finally:
            output_path.unlink(missing_ok=True)


class CopilotAdapter(AgentAdapter):
    name = "copilot"
    display_name = "GitHub Copilot CLI"
    capabilities = AgentCapabilities(True, False, True, ("default", "acceptEdits", "bypassPermissions"))

    @staticmethod
    def _windows_candidates() -> list[Path]:
        appdata = Path(os.environ.get("APPDATA", ""))
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = [appdata / "npm" / "copilot.cmd", appdata / "npm" / "copilot", local / "Microsoft" / "WinGet" / "Links" / "copilot.exe", local / "Microsoft" / "WindowsApps" / "copilot.exe"]
        pkg = _latest_matching_dir(local / "Microsoft" / "WinGet" / "Packages", "GitHub.Copilot*")
        if pkg is not None:
            candidates.append(pkg / "copilot.exe")
        return candidates

    def resolve_executable(self) -> str:
        return _resolve_command("copilot", env=self.subprocess_env(), env_vars=("COPILOT_CLI", "COPILOT_PATH"), windows_candidates=self._windows_candidates)

    def _cli_not_found_message(self) -> str:
        return "❌ `copilot` CLI not found.\nInstall GitHub Copilot CLI and make sure `copilot` is available in `PATH`."

    def build_cmd(self, prompt: str, continue_session: bool, resume_id: str | None, *, streaming: bool = False, permission_mode: str = "default") -> list[str]:
        cmd = [self.resolve_executable(), "--prompt", prompt, "--silent", "--no-color", "--stream", "on" if streaming else "off"]
        if resume_id:
            cmd += ["--resume", resume_id]
        elif continue_session:
            cmd.append("--continue")
        mode = self.normalize_permission_mode(permission_mode)
        if mode in {"default", "acceptEdits"}:
            cmd.append("--allow-all-tools")
        elif mode == "bypassPermissions":
            cmd += ["--allow-all-tools", "--allow-all-paths"]
        return cmd

    async def run_batch(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, timeout: int | None = None, permission_mode: str = "default", on_process: Callable[[object], None] | None = None) -> AgentRunResult:
        effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
        cmd = self.build_cmd(prompt, continue_session, resume_id, streaming=False, permission_mode=permission_mode)
        try:
            if os.name == "nt":
                preview, _stdout, stderr_text, completed = await _run_windows_file_process(cmd, cwd=workdir, env=self.subprocess_env(), prefix=self.name, timeout=effective_timeout, on_process=on_process)
                if not completed:
                    return AgentRunResult(f"⏱ Timeout after {effective_timeout}s.")
                output = preview or (f"⚠️ stderr:\n{stderr_text}" if stderr_text else "")
                return AgentRunResult(output or "_(no output)_")
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workdir, env=self.subprocess_env())
            if on_process:
                on_process(proc)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return AgentRunResult(f"⏱ Timeout after {effective_timeout}s.")
            output = stdout.decode(errors="replace").strip()
            if not output and stderr:
                output = f"⚠️ stderr:\n{stderr.decode(errors='replace').strip()}"
            return AgentRunResult(output or "_(no output)_")
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            return AgentRunResult(self._cli_not_found_message())
        except Exception as exc:
            logger.exception("Unexpected error in Copilot batch execution")
            return AgentRunResult(f"❌ Error: {exc}")

    async def run_stream(self, prompt: str, *, workdir: str, continue_session: bool = False, resume_id: str | None = None, on_update: Callable[[str], Awaitable[None]] | None = None, stream_interval: float = 2.0, timeout: int | None = None, permission_mode: str = "default", cancel_event: asyncio.Event | None = None, on_process: Callable[[object], None] | None = None) -> AgentRunResult:
        effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
        cmd = self.build_cmd(prompt, continue_session, resume_id, streaming=True, permission_mode=permission_mode)
        try:
            if os.name == "nt":
                preview, _stdout, stderr_text, completed = await _run_windows_file_process(cmd, cwd=workdir, env=self.subprocess_env(), prefix=self.name, timeout=effective_timeout, on_update=on_update, stream_interval=stream_interval, cancel_event=cancel_event, on_process=on_process)
                if not completed:
                    return AgentRunResult(preview or f"⏱ Timeout after {effective_timeout}s.")
                output = preview or (f"⚠️ stderr:\n{stderr_text}" if stderr_text else "")
                return AgentRunResult(output or "_(no output)_")
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workdir, env=self.subprocess_env())
            if on_process:
                on_process(proc)
            deadline = asyncio.get_running_loop().time() + effective_timeout
            accumulated = ""
            last_update = 0.0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    proc.kill()
                    raise asyncio.CancelledError
                now = asyncio.get_running_loop().time()
                if now > deadline:
                    proc.kill()
                    return AgentRunResult(accumulated or f"⏱ Timeout after {effective_timeout}s.")
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(256), timeout=min(1.0, max(0.1, deadline - now)))
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
                accumulated += chunk.decode(errors="replace")
                now = asyncio.get_running_loop().time()
                if on_update and accumulated and (now - last_update) >= stream_interval:
                    try:
                        await on_update(accumulated)
                    except Exception:
                        pass
                    last_update = now
            await proc.wait()
            result = accumulated.strip()
            if not result:
                stderr_raw = await proc.stderr.read()
                if stderr_raw:
                    result = f"⚠️ stderr:\n{stderr_raw.decode(errors='replace').strip()}"
            return AgentRunResult(result or "_(no output)_")
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            return AgentRunResult(self._cli_not_found_message())
        except Exception as exc:
            logger.exception("Unexpected error in Copilot stream execution")
            return AgentRunResult(f"❌ Error: {exc}")


_ADAPTERS: dict[str, AgentAdapter] = {"claude": ClaudeCodeAdapter(), "codex": CodexAdapter(), "copilot": CopilotAdapter()}


def get_agent(name: str = "claude") -> AgentAdapter:
    key = name.lower()
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise KeyError(f"Unknown agent '{name}'. Available: {', '.join(sorted(_ADAPTERS))}") from exc


def available_agents() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
