"""
Telegram → local coding CLI bridge
Receives text and voice messages from Telegram and runs Claude Code, Codex CLI,
or GitHub Copilot CLI on the local PC.

Execution modes:
  • Normal / voice message  → inline execution (reply in the same chat, bot waits)
  • /task <prompt>           → background execution with push notification on completion
"""

from __future__ import annotations

import asyncio
import fnmatch
import io
import json
import logging
import os
import re
import tempfile
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from vibeaway import config
from vibeaway.agents import available_agents, get_agent
from vibeaway.transcriber import transcribe_audio
from vibeaway.session_manager import (
    find_session,
    format_session_list,
    list_sessions,
    load_session_state,
    read_last_interaction,
)
from vibeaway.tts import synthesize as tts_synthesize
from vibeaway.paths import BOT_LOG
from vibeaway.locales import t

_LOG_FILE = BOT_LOG

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.WARNING,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.getLogger("config").setLevel(logging.INFO)
logging.getLogger("session_manager").setLevel(logging.INFO)
logging.getLogger("transcriber").setLevel(logging.INFO)

_CLAUDE_AGENT = get_agent("claude")
_AGENT_CHOICES = list(available_agents())
_PERMISSION_MODE_CHOICES = ["default", "acceptEdits", "bypassPermissions"]


# ─── Usage tracking ───────────────────────────────────────────────────────────

_last_usage: dict = {}
"""Metadata from last agent response (tokens, cost, duration)."""

_last_prompt: str = ""
_last_response: str = ""
_last_agent_name: str = ""
"""Last prompt/response cached for the most recently used agent."""


# ─── Safety checker for voice transcriptions ──────────────────────────────────

# Dangerous patterns grouped by category.
# Each entry: (regex, human-readable label).
# Match is case-insensitive on whole words or significant prefixes.
# Base danger patterns — language-neutral (commands, shell syntax, file paths)
_DANGER_PATTERNS: list[tuple[re.Pattern, str]] = [
    # File deletion commands (universal)
    (re.compile(r"\b(rm|rmdir|del|delete|wipe|shred|truncat)\b", re.I), "delete command"),
    (re.compile(r"\b(drop\s+(table|database|schema|index))\b", re.I), "drop database"),

    # Privileged execution (universal)
    (re.compile(r"\b(sudo|su\s|chmod|chown|passwd|visudo)\b", re.I), "privileged command"),
    (re.compile(r"\b(run\s+as\s+root|privilege\s+escalat)\b", re.I), "privilege escalation"),

    # Network / exfiltration (universal)
    (re.compile(r"\b(curl|wget|nc|netcat|ncat)\b.*\b(http|ftp|ssh|tcp)\b", re.I), "network transfer"),
    (re.compile(r"\b(upload\s+to|exfiltrat)\b", re.I), "data exfiltration"),

    # Shell commands (universal)
    (re.compile(r"\b(eval|exec|system|popen|subprocess)\s*\(", re.I), "shell execution"),
    (re.compile(r"(&&|\|\||;\s*rm|;\s*del|`[^`]+`|\$\([^)]+\))", re.I), "shell command chaining"),

    # Credential files (universal)
    (re.compile(r"\b(\.env|\.ssh|authorized_keys|id_rsa|private.?key|secret)\b", re.I), "credential file"),
    (re.compile(r"\b(overwrite|rewrite)\b.*\b(config|secret|key|token|password)\b", re.I), "credential overwrite"),
]


def _load_danger_patterns() -> None:
    """Extend danger patterns with locale-specific patterns from the active locale."""
    from vibeaway.locales import load_locale
    lang = config.WHISPER_LANGUAGE
    if not lang:
        return
    locale_patterns = load_locale(lang).get("danger_patterns", [])
    for entry in locale_patterns:
        try:
            _DANGER_PATTERNS.append(
                (re.compile(entry["pattern"], re.I), entry["label"])
            )
        except (KeyError, re.error) as exc:
            logger.warning("Invalid danger pattern in locale '%s': %s", lang, exc)

# Callback data for confirmation buttons
_CB_CONFIRM = "safety:confirm"
_CB_CANCEL  = "safety:cancel"
_CB_MODIFY  = "safety:modify"
# Key in user_data where pending payload is parked awaiting confirmation
_PENDING_KEY = "safety_pending"
_MODIFY_KEY  = "safety_awaiting_modify"

# Input history — per-user circular buffer
_HISTORY_KEY = "input_history"
_HISTORY_MAX = 50


def _get_history(context: ContextTypes.DEFAULT_TYPE) -> deque:
    """Return the per-user input history deque, creating it if needed."""
    hist = context.user_data.get(_HISTORY_KEY)
    if not isinstance(hist, deque):
        hist = deque(maxlen=_HISTORY_MAX)
        context.user_data[_HISTORY_KEY] = hist
    return hist

# Voice trigger words — base set, expanded by locale at startup
_TRIGGER_WORDS: set[str] = {"ok"}

# Localized descriptions for help text (populated by _load_locale)
_LOCALE_DESCRIPTIONS: dict[str, str] = {}


def _load_locale() -> None:
    """Load command aliases, trigger words, and UI messages from the locale."""
    from vibeaway.locales import get_aliases, get_trigger_words, get_descriptions, init_locale

    lang = config.WHISPER_LANGUAGE
    if not lang:
        return

    # Initialize message translations and locale-specific danger patterns
    init_locale(lang)
    _load_danger_patterns()

    # Add trigger words
    for tw in get_trigger_words(lang):
        _TRIGGER_WORDS.add(tw.lower())

    # Add aliases to existing commands
    aliases_map = get_aliases(lang)
    for cmd in _CMD_REGISTRY:
        locale_aliases = aliases_map.get(cmd.name, [])
        for alias in locale_aliases:
            if alias not in cmd.aliases:
                cmd.aliases.append(alias)

    # Store localized descriptions
    _LOCALE_DESCRIPTIONS.update(get_descriptions(lang))

    if aliases_map:
        logger.info("Locale '%s' loaded: %d commands with aliases, %d trigger words",
                     lang, len(aliases_map), len(_TRIGGER_WORDS))


def check_safety(text: str) -> list[str]:
    """
    Returns the list of risk categories detected in the text.
    Empty list → text is safe.
    """
    hits = []
    for pattern, label in _DANGER_PATTERNS:
        if pattern.search(text) and label not in hits:
            hits.append(label)
    return hits


def _confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(t("safety_confirm"), callback_data=_CB_CONFIRM),
        InlineKeyboardButton(t("safety_modify"),  callback_data=_CB_MODIFY),
        InlineKeyboardButton(t("safety_cancel"),  callback_data=_CB_CANCEL),
    ]])


async def handle_safety_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles tap on ✅/✏️/❌ buttons of the safety confirmation prompt.
    Retrieves the payload parked in user_data and executes, modifies, or discards.
    """
    query = update.callback_query
    await query.answer()

    pending = context.user_data.pop(_PENDING_KEY, None)

    if query.data == _CB_CANCEL or pending is None:
        logger.info("Safety callback: user cancelled (user_id=%d)", query.from_user.id)
        context.user_data.pop(_MODIFY_KEY, None)
        await query.edit_message_text(t("safety_cancelled"))
        return

    if query.data == _CB_MODIFY and pending:
        logger.info("Safety callback: user chose modify (user_id=%d)", query.from_user.id)
        context.user_data[_MODIFY_KEY] = True
        await query.edit_message_text(
            t("safety_modify_prompt", text=pending['transcript']),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    logger.info("Safety callback: user confirmed (user_id=%d)", query.from_user.id)
    await query.edit_message_text(
        t("safety_confirmed", text=pending['transcript']),
        parse_mode=ParseMode.MARKDOWN,
    )

    class _FakeUpdate:
        def __init__(self, msg):
            self.message = msg
            self.effective_user = query.from_user
            self.effective_chat = query.message.chat

    fake_update = _FakeUpdate(query.message)
    await _process_prompt(fake_update, context, pending["transcript"], reply_voice=pending.get("voice", False))


def is_authorized(user_id: int) -> bool:
    if not config.ALLOWED_USER_IDS:
        return True
    allowed = user_id in config.ALLOWED_USER_IDS
    if not allowed:
        logger.warning("Access denied for user_id=%d", user_id)
    return allowed


def get_display_name(user) -> str:
    """Return configured name from ALLOWED_USERS, or Telegram first_name as fallback."""
    if user and user.id in config.ALLOWED_USERS:
        configured = config.ALLOWED_USERS[user.id]
        if configured:
            return configured
    if user:
        return user.first_name or user.username or str(user.id)
    return ""


def _resolve_workdir(raw: str) -> tuple[str | None, str]:
    """
    Resolves a path relative to BASEDIR and checks it stays inside.
    Returns (resolved_path, error). If error is empty, the path is valid.
    """
    basedir = Path(config.BASEDIR).resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = basedir / candidate
    resolved = candidate.resolve()
    if not str(resolved).lower().startswith(str(basedir).lower()):
        return None, f"Path must stay inside `{_short_path(basedir)}`"
    if not resolved.is_dir():
        return None, f"Directory not found: `{_short_path(resolved)}`"
    return str(resolved), ""


def _normalize_agent_name(name: str | None) -> str:
    if name and name.lower() in _AGENT_CHOICES:
        return name.lower()
    return "claude"


def _agent_for_name(name: str | None):
    return get_agent(_normalize_agent_name(name))


def _current_agent_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    return _normalize_agent_name(_get(context, "agent"))


def _current_agent(context: ContextTypes.DEFAULT_TYPE):
    return _agent_for_name(_current_agent_name(context))


def _current_agent_label(context: ContextTypes.DEFAULT_TYPE) -> str:
    return _current_agent(context).display_name


def _effective_permission_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    raw_value = _get(context, "permission_mode")
    return _current_agent(context).normalize_permission_mode(raw_value)


# ─── Task Manager ─────────────────────────────────────────────────────────────

class TaskStatus(Enum):
    RUNNING   = "running"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"


@dataclass
class BackgroundTask:
    task_id: int
    prompt: str
    workdir: str
    chat_id: int
    agent_name: str = "claude"
    start_time: float = field(default_factory=time.monotonic)
    status: TaskStatus = TaskStatus.RUNNING
    output: str = ""
    end_time: float | None = None
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False, compare=False)

    @property
    def elapsed(self) -> str:
        end = self.end_time or time.monotonic()
        secs = int(end - self.start_time)
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if m else f"{s}s"

    @property
    def status_emoji(self) -> str:
        return {
            TaskStatus.RUNNING:   "⏳",
            TaskStatus.DONE:      "✅",
            TaskStatus.ERROR:     "❌",
            TaskStatus.CANCELLED: "🚫",
        }[self.status]


class TaskManager:
    """Manages background tasks for all supported CLI agents."""
    MAX_HISTORY = 20

    def __init__(self) -> None:
        self._tasks: dict[int, BackgroundTask] = {}
        self._counter = 0

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def create(
        self,
        prompt: str,
        workdir: str,
        chat_id: int,
        agent_name: str = "claude",
    ) -> BackgroundTask:
        task = BackgroundTask(
            task_id=self._next_id(),
            prompt=prompt,
            workdir=workdir,
            chat_id=chat_id,
            agent_name=agent_name,
        )
        self._tasks[task.task_id] = task
        self._prune()
        logger.info("Task #%d created (chat=%d, workdir=%s)", task.task_id, chat_id, workdir)
        return task

    def get(self, task_id: int) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def for_chat(self, chat_id: int) -> list[BackgroundTask]:
        return sorted(
            [t for t in self._tasks.values() if t.chat_id == chat_id],
            key=lambda t: t.task_id,
            reverse=True,
        )

    def running_for_chat(self, chat_id: int) -> list[BackgroundTask]:
        return [t for t in self.for_chat(chat_id) if t.status == TaskStatus.RUNNING]

    def _prune(self) -> None:
        completed = sorted(
            [t for t in self._tasks.values() if t.status != TaskStatus.RUNNING],
            key=lambda t: t.task_id,
        )
        for old in completed[: max(0, len(completed) - self.MAX_HISTORY)]:
            del self._tasks[old.task_id]


# Global instance shared across all handlers
task_manager = TaskManager()


# ─── CLI agents (adapter-backed wrappers) ────────────────────────────────────

def _build_cmd(
    prompt: str,
    continue_session: bool,
    resume_id: str | None,
    streaming: bool = False,
    permission_mode: str = "bypassPermissions",
) -> list[str]:
    """Backwards-compatible wrapper around the Claude adapter command builder."""
    return _CLAUDE_AGENT.build_cmd(
        prompt,
        continue_session,
        resume_id,
        streaming=streaming,
        permission_mode=permission_mode,
    )


def _subprocess_env() -> dict[str, str]:
    """Backwards-compatible wrapper used by tests and current bot code."""
    return _CLAUDE_AGENT.subprocess_env()


def _resolve_claude() -> str:
    """Backwards-compatible wrapper used by tests and current bot code."""
    return _CLAUDE_AGENT.resolve_executable()


def _resolve_agent(name: str) -> str:
    return _agent_for_name(name).resolve_executable()


async def run_agent_async(
    agent_name: str,
    prompt: str,
    workdir: str,
    continue_session: bool = False,
    resume_id: str | None = None,
    bg_task: BackgroundTask | None = None,
    timeout: int | None = None,
    permission_mode: str = "bypassPermissions",
) -> str:
    """Blocking execution for the selected agent (used by background tasks)."""
    effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
    agent = _agent_for_name(agent_name)

    def _stash_process(proc: asyncio.subprocess.Process) -> None:
        if bg_task is not None:
            bg_task._process = proc

    result = await agent.run_batch(
        prompt,
        workdir=workdir,
        continue_session=continue_session,
        resume_id=resume_id,
        timeout=effective_timeout,
        permission_mode=permission_mode,
        on_process=_stash_process,
    )
    return result.text


async def run_claude_async(
    prompt: str,
    workdir: str,
    continue_session: bool = False,
    resume_id: str | None = None,
    bg_task: BackgroundTask | None = None,
    timeout: int | None = None,
    permission_mode: str = "bypassPermissions",
) -> str:
    """Blocking execution (used by background tasks)."""
    return await run_agent_async(
        "claude",
        prompt,
        workdir,
        continue_session=continue_session,
        resume_id=resume_id,
        bg_task=bg_task,
        timeout=timeout,
        permission_mode=permission_mode,
    )


# ─── User settings ────────────────────────────────────────────────────────────
SETTINGS_SCHEMA: dict[str, dict] = {
    "agent": {
        "type": "choice",
        "default": lambda: _normalize_agent_name(config.DEFAULT_AGENT),
        "choices": _AGENT_CHOICES,
        "description": "CLI agent used for prompts, tasks, sessions, and resume",
        "example": "codex",
    },
    "homedir": {
        "type": "path",
        "default": lambda: config.WORKDIR,
        "description": "Working directory (⌂)",
        "example": "my-project",
    },
    "stream_interval": {
        "type": "float",
        "default": 2.0,
        "min": 0.5,
        "max": 10.0,
        "description": "Streaming update frequency (seconds)",
        "example": "1.5",
    },
    "timeout": {
        "type": "int",
        "default": lambda: config.CLAUDE_TIMEOUT_SECONDS,
        "min": 10,
        "max": 600,
        "description": "Timeout for inline prompts (seconds)",
        "example": "180",
    },
    "task_timeout": {
        "type": "int",
        "default": lambda: config.CLAUDE_TASK_TIMEOUT_SECONDS,
        "min": 60,
        "max": 7200,
        "description": "Timeout for background /task (seconds)",
        "example": "1800",
    },
    "language": {
        "type": "choice",
        "default": lambda: config.WHISPER_LANGUAGE,
        "choices": ["it", "en", "fr", "de", "es", "pt"],
        "description": "Voice message language (Whisper transcription)",
        "example": "en",
    },
    "permission_mode": {
        "type": "choice",
        "default": "bypassPermissions",
        "choices": _PERMISSION_MODE_CHOICES,
        "description": "CLI execution mode (behavior depends on the current agent)",
        "example": "acceptEdits",
    },
}


def _get(context: ContextTypes.DEFAULT_TYPE, key: str):
    """Reads a user setting, falling back to the schema default."""
    schema = SETTINGS_SCHEMA[key]
    default = schema["default"]
    default_val = default() if callable(default) else default
    return context.user_data.get(f"setting_{key}", default_val)


def _short_path(path) -> str:
    """Replace the BASEDIR prefix with ``⌂`` for shorter display.

    Example: ``/home/user/projects/myapp`` → ``⌂/myapp``
    """
    s = str(path)
    base = config.BASEDIR.rstrip(os.sep)
    if s == base:
        return "⌂"
    prefix = base + os.sep
    if s.startswith(prefix):
        return "⌂" + os.sep + s[len(prefix):]
    return s


def _settings_panel(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Formats the settings panel for Telegram."""
    lines = ["⚙️ *Current settings*\n"]
    for key, schema in SETTINGS_SCHEMA.items():
        value = _get(context, key)
        if key == "agent":
            value = f"{value} ({_agent_for_name(str(value)).display_name})"
        elif key == "permission_mode":
            value = f"{value} → {_effective_permission_mode(context)}"
        is_custom = f"setting_{key}" in context.user_data
        marker = " ✏️" if is_custom else ""
        lines.append(
            f"• `{key}` = `{value}`{marker}\n"
            f"  _{schema['description']}_\n"
        )
    lines.append(
        "\n*Change:* `/set <param> <value>`\n"
        "*Quick switch:* `/agent <name>`\n"
        "*Reset:* `/set <param> reset`\n"
        "*Params:* " + ", ".join(f"`{k}`" for k in SETTINGS_SCHEMA)
    )
    return "\n".join(lines)


async def run_agent_streaming(
    agent_name: str,
    prompt: str,
    workdir: str,
    continue_session: bool = False,
    resume_id: str | None = None,
    on_update: Callable[[str], Awaitable[None]] | None = None,
    stream_interval: float = 2.0,
    timeout: int | None = None,
    permission_mode: str = "bypassPermissions",
    cancel_event: asyncio.Event | None = None,
    proc_holder: list | None = None,
) -> str:
    """
    Streaming execution for the selected agent.

    cancel_event: when set externally, kills the subprocess and raises CancelledError.
    proc_holder: single-element list filled with the subprocess once started (for /bg).
    """
    effective_timeout = timeout if timeout is not None else config.CLAUDE_TIMEOUT_SECONDS
    agent = _agent_for_name(agent_name)

    def _stash_process(proc: asyncio.subprocess.Process) -> None:
        if proc_holder is not None:
            proc_holder.append(proc)

    try:
        result = await agent.run_stream(
            prompt,
            workdir=workdir,
            continue_session=continue_session,
            resume_id=resume_id,
            on_update=on_update,
            stream_interval=stream_interval,
            timeout=effective_timeout,
            permission_mode=permission_mode,
            cancel_event=cancel_event,
            on_process=_stash_process,
        )
        if result.usage:
            _last_usage.clear()
            _last_usage.update(result.usage)
            _last_usage["agent_name"] = agent_name
        elif _last_usage.get("agent_name") == agent_name:
            _last_usage.clear()
        return result.text

    except asyncio.CancelledError:
        try:
            if proc_holder:
                proc_holder[0].kill()
        except Exception:
            pass
        raise


async def run_claude_streaming(
    prompt: str,
    workdir: str,
    continue_session: bool = False,
    resume_id: str | None = None,
    on_update: Callable[[str], Awaitable[None]] | None = None,
    stream_interval: float = 2.0,
    timeout: int | None = None,
    permission_mode: str = "bypassPermissions",
    cancel_event: asyncio.Event | None = None,
    proc_holder: list | None = None,
) -> str:
    return await run_agent_streaming(
        "claude",
        prompt,
        workdir,
        continue_session=continue_session,
        resume_id=resume_id,
        on_update=on_update,
        stream_interval=stream_interval,
        timeout=timeout,
        permission_mode=permission_mode,
        cancel_event=cancel_event,
        proc_holder=proc_holder,
    )


# ─── Utility Telegram ──────────────────────────────────────────────────────────

def split_message(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        cut = text.rfind("\n", 0, max_len) if len(text) > max_len else len(text)
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def send_chunks(update: Update, text: str) -> None:
    for chunk in split_message(text):
        await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)


async def push_chunks(bot, chat_id: int, text: str) -> None:
    """Sends proactive messages (no Update, used for background push notifications)."""
    for chunk in split_message(text):
        await bot.send_message(chat_id, chunk, parse_mode=ParseMode.MARKDOWN)


# ─── Background task runner ────────────────────────────────────────────────────

async def _run_background_task(
    bg_task: BackgroundTask,
    bot,
    continue_session: bool,
    resume_id: str | None,
    permission_mode: str = "bypassPermissions",
    timeout: int | None = None,
) -> None:
    """
    Runs as a separate asyncio.Task (fire-and-forget).
    Sends push notification on completion, error, or cancellation.
    """
    effective_timeout = timeout if timeout is not None else config.CLAUDE_TASK_TIMEOUT_SECONDS
    agent = _agent_for_name(bg_task.agent_name)
    try:
        output = await run_agent_async(
            bg_task.agent_name,
            bg_task.prompt,
            workdir=bg_task.workdir,
            continue_session=continue_session,
            resume_id=resume_id,
            bg_task=bg_task,
            timeout=effective_timeout,
            permission_mode=permission_mode,
        )
        bg_task.status = TaskStatus.DONE
        bg_task.output = output
        bg_task.end_time = time.monotonic()

        preview = bg_task.prompt[:60] + ("…" if len(bg_task.prompt) > 60 else "")
        header = (
            f"✅ *Task #{bg_task.task_id} completed* ({bg_task.elapsed})\n"
            f"🤖 `{agent.display_name}`\n"
            f"📁 `{Path(bg_task.workdir).name}`\n"
            f"💬 _{preview}_\n\n"
        )
        await push_chunks(bot, bg_task.chat_id, header + output)

    except asyncio.CancelledError:
        bg_task.status = TaskStatus.CANCELLED
        bg_task.end_time = time.monotonic()
        await bot.send_message(
            bg_task.chat_id,
            f"🚫 *Task #{bg_task.task_id} cancelled* ({bg_task.elapsed})",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as exc:
        bg_task.status = TaskStatus.ERROR
        bg_task.output = str(exc)
        bg_task.end_time = time.monotonic()
        logger.exception("Error in background task #%d", bg_task.task_id)
        await bot.send_message(
            bg_task.chat_id,
            f"❌ *Task #{bg_task.task_id} failed* ({bg_task.elapsed})\n`{exc}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─── Command registry ─────────────────────────────────────────────────────────
#
# Each command is described once.  The rest of the code derives help text,
# usage messages, CommandHandler registrations and voice-command aliases from
# this single source of truth.
#
#   name     – Telegram /command name (without the slash)
#   group    – section header shown in /help
#   brief    – one-line description shown in /help
#   howto    – longer usage hint shown when the command is called without args
#              (may contain Markdown).  None → command needs no arguments.
#   aliases  – voice / localized aliases (list of strings)
#   handler  – filled in later by _register_cmd decorator

@dataclass
class CmdDef:
    name: str
    group: str
    brief: str
    howto: str | None = None
    shortcuts: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    handler: Callable | None = None

_CMD_REGISTRY: list[CmdDef] = []
_CMD_BY_NAME: dict[str, CmdDef] = {}

# Group ordering for the help message
_GROUP_ORDER = ["Sessions", "Background tasks", "Navigation & files", "Settings & info"]

def _register_cmd(name: str, group: str, brief: str, *,
                  howto: str | None = None, shortcuts: list[str] | None = None,
                  aliases: list[str] | None = None):
    """Decorator: register a command handler in the central registry."""
    def decorator(func):
        cmd = CmdDef(
            name=name, group=group, brief=brief,
            howto=howto, shortcuts=shortcuts or [],
            aliases=aliases or [], handler=func,
        )
        _CMD_REGISTRY.append(cmd)
        _CMD_BY_NAME[name] = cmd
        for sc in cmd.shortcuts:
            _CMD_BY_NAME[sc] = cmd
        return func
    return decorator

def _cmd_usage(name: str) -> str:
    """Return the howto/usage string for a command, looked up from the registry."""
    cmd = _CMD_BY_NAME.get(name)
    if cmd and cmd.howto:
        return cmd.howto
    return f"Usage: `/{name}`"

def _build_help_text() -> str:
    """Build the help listing grouped by section, with shortcuts and voice aliases."""
    hidden = {"start", "help"}
    groups: dict[str, list[str]] = {}
    for cmd in _CMD_REGISTRY:
        if cmd.name in hidden:
            continue
        if cmd.group not in groups:
            groups[cmd.group] = []
        line = f"• /{cmd.name}"
        if cmd.shortcuts:
            line += " (`" + "`, `".join(f"/{s}" for s in cmd.shortcuts) + "`)"
        if cmd.howto:
            m = re.search(r"/\w+\s+(.*?)`", cmd.howto.split("\n")[0])
            if m:
                line += f" `{m.group(1)}`"
        brief = _LOCALE_DESCRIPTIONS.get(cmd.name, cmd.brief)
        line += f" — {brief}"
        if cmd.aliases:
            line += " 🎙 _" + ", ".join(cmd.aliases) + "_"
        groups[cmd.group].append(line)

    sections = []
    for g in _GROUP_ORDER:
        if g in groups:
            sections.append(f"*{g}*\n" + "\n".join(groups[g]))

    tips = (
        "*Tips*\n"
        "• Chain commands/prompts with ` .+ ` separator:\n"
        "  `.ls .+ show me the README`\n"
        "  `explain the config .+ now refactor it`"
    )
    sections.append(tips)

    return "\n\n".join(sections)


# ─── Handler ───────────────────────────────────────────────────────────────────

@_register_cmd("start", "Sessions", "show this help")
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("/start from user_id=%d", update.effective_user.id)
    name = get_display_name(update.effective_user)
    greeting = f"Hey {name}! " if name else ""
    agent_label = _current_agent_label(context)
    await update.message.reply_text(
        f"🤖 *Telegram CLI Bot* active!\n\n"
        f"{greeting}Current agent: *{agent_label}*.\n"
        f"Send me text or voice to interact with it on your PC.\n\n"
        + _build_help_text(),
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("help", "Sessions", "show this help", shortcuts=["h"])
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


@_register_cmd("reset", "Sessions", "new conversation", shortcuts=["new"])
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("/reset from user_id=%d", update.effective_user.id)
    context.user_data["continue_session"] = False
    context.user_data.pop("resume_id", None)
    name = get_display_name(update.effective_user)
    msg = f"🔄 Ok {name}, session reset." if name else "🔄 Session reset."
    await update.message.reply_text(msg + " Next message → new conversation.")


@_register_cmd("sessions", "Sessions", "list saved sessions",
                howto="Usage: `/sessions [N]`\nDefault: 2 most recent.",
                shortcuts=["ss"],
                )
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    args = list(context.args) if context.args else []
    limit = 2
    for a in args:
        # Accept both "5" and "-5" for backward compat
        m = re.match(r"^-?(\d+)$", a)
        if m:
            limit = max(1, int(m.group(1)))

    workdir = _get(context, "homedir")
    current_id = context.user_data.get("resume_id")
    agent_name = _current_agent_name(context)

    wait_msg = await update.message.reply_text(t("loading_sessions"))

    loop = asyncio.get_running_loop()
    sessions = await loop.run_in_executor(None, list_sessions, workdir, limit, agent_name)
    if not sessions:
        sessions = await loop.run_in_executor(None, list_sessions, None, limit, agent_name)

    text = format_session_list(sessions, current_session_id=current_id, backend=agent_name)
    await wait_msg.delete()
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


@_register_cmd("resume", "Sessions", "resume session",
                howto="Usage: `/resume <id or title>`\n\nSee sessions with /sessions",
                shortcuts=["re"],
                )
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    if not context.args:
        await update.message.reply_text(
            _cmd_usage("resume"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    query = " ".join(context.args)
    workdir = _get(context, "homedir")
    agent_name = _current_agent_name(context)

    wait_msg = await update.message.reply_text(t("searching", query=query), parse_mode=ParseMode.MARKDOWN)

    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, find_session, query, workdir, agent_name)
    if session is None:
        session = await loop.run_in_executor(None, find_session, query, None, agent_name)

    await wait_msg.delete()

    if session is None:
        await update.message.reply_text(
            t("session_not_found", query=query),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data["resume_id"] = session.session_id
    context.user_data["session_title"] = session.title
    context.user_data["continue_session"] = False

    # Pre-load usage/prompt/response from the current backend session store.
    state_workdir = session.project_dir or _get(context, "homedir")
    state = load_session_state(session.session_id, state_workdir, backend=agent_name)
    if state is not None:
        global _last_agent_name, _last_prompt, _last_response
        _last_agent_name = agent_name
        _last_prompt = state.prompt
        _last_response = state.response
        if state.usage:
            _last_usage.clear()
            _last_usage.update(state.usage)
            _last_usage["agent_name"] = agent_name
        elif _last_usage.get("agent_name") == agent_name:
            _last_usage.clear()

    dir_note = ""
    if session.project_dir and Path(session.project_dir).is_dir():
        context.user_data["homedir"] = session.project_dir
        context.user_data["setting_homedir"] = session.project_dir
        dir_note = f"\n📁 Directory set to: `{_short_path(session.project_dir)}`"

    await update.message.reply_text(
        f"✅ *Session selected*\n\n"
        f"🆔 `{session.session_id[:8]}…`\n"
        f"💬 _{session.title}_\n"
        f"🕐 {session.age_str}{dir_note}\n\n"
        f"Next message will continue this session.",
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("task", "Background tasks", "run in background", shortcuts=["t"],
                howto="Usage: `/task <prompt>`\n\nExample:\n`/task Analyze all Python files in src and report any bugs`")
async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a background task for the current CLI agent."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    if not context.args:
        await update.message.reply_text(
            _cmd_usage("task"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    prompt = " ".join(context.args)
    workdir = _get(context, "homedir")
    perm_mode = _effective_permission_mode(context)
    task_timeout = _get(context, "task_timeout")
    chat_id = update.effective_chat.id
    continue_session = context.user_data.get("continue_session", False)
    resume_id = context.user_data.get("resume_id")
    agent_name = _current_agent_name(context)
    agent_label = _current_agent_label(context)

    bg_task = task_manager.create(
        prompt=prompt,
        workdir=workdir,
        chat_id=chat_id,
        agent_name=agent_name,
    )

    asyncio_task = asyncio.create_task(
        _run_background_task(bg_task, context.bot, continue_session, resume_id, perm_mode, timeout=task_timeout)
    )
    bg_task._asyncio_task = asyncio_task

    if resume_id:
        context.user_data.pop("resume_id", None)
    context.user_data["continue_session"] = True

    preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
    await update.message.reply_text(
        f"🚀 *Task #{bg_task.task_id} started*\n\n"
        f"🤖 `{agent_label}`\n"
        f"💬 _{preview}_\n"
        f"📁 `{Path(workdir).name}`\n\n"
        f"You'll get a notification when {agent_label} is done.\n"
        f"• /tasks — check status\n"
        f"• /cancel {bg_task.task_id} — abort",
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("tasks", "Background tasks", "list running/recent tasks", shortcuts=["jobs"])
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists running and recent tasks for this chat."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    chat_id = update.effective_chat.id
    tasks = task_manager.for_chat(chat_id)

    if not tasks:
        await update.message.reply_text(
            "📭 No tasks found.\n\nUse `/task <prompt>` to start one in background.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = ["📋 *Recent tasks*\n"]
    for t in tasks[:10]:
        if t.status == TaskStatus.RUNNING:
            elapsed_str = f"{int(time.monotonic() - t.start_time)}s running"
        else:
            elapsed_str = t.elapsed
        preview = t.prompt[:50] + ("…" if len(t.prompt) > 50 else "")
        lines.append(
            f"{t.status_emoji} *#{t.task_id}* — {elapsed_str}\n"
            f"   `{t.agent_name}` · `{Path(t.workdir).name}` · _{preview}_\n"
        )

    running = task_manager.running_for_chat(chat_id)
    if running:
        lines.append(f"\n_To cancel: /cancel {running[0].task_id}_")

    await send_chunks(update, "\n".join(lines))


@_register_cmd("cancel", "Background tasks", "cancel a task", shortcuts=["kill"],
                howto="Usage: `/cancel [id]`",
                )
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a running task — if id omitted, cancels the most recent."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    chat_id = update.effective_chat.id
    running = task_manager.running_for_chat(chat_id)

    if not running:
        await update.message.reply_text(t("no_tasks"))
        return

    if context.args:
        try:
            task_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(_cmd_usage("cancel"), parse_mode=ParseMode.MARKDOWN)
            return
        bg_task = task_manager.get(task_id)
        if bg_task is None or bg_task.chat_id != chat_id:
            await update.message.reply_text(f"❌ Task #{task_id} not found.")
            return
        if bg_task.status != TaskStatus.RUNNING:
            await update.message.reply_text(
                f"ℹ️ Task #{task_id} is not running (status: {bg_task.status.value})."
            )
            return
    else:
        bg_task = running[0]

    # Kill the OS subprocess
    if bg_task._process and bg_task._process.returncode is None:
        try:
            bg_task._process.kill()
        except ProcessLookupError:
            pass

    # Cancel the asyncio task (triggers CancelledError → push notification)
    if bg_task._asyncio_task and not bg_task._asyncio_task.done():
        bg_task._asyncio_task.cancel()

    await update.message.reply_text(
        f"⏹ Cancelling Task #{bg_task.task_id}…\n"
        f"You'll get a confirmation shortly.",
    )


@_register_cmd("bg", "Background tasks", "move to background",
                howto="Usage: `/bg`\n\nSends the currently running inline prompt to background.\nYou'll receive a push notification when it completes.",
                aliases=[],
                )
async def cmd_bg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Move the active inline execution to a background task."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    active = context.user_data.get("active_inline")
    if not active:
        await update.message.reply_text(t("bg_not_running"), parse_mode=ParseMode.MARKDOWN)
        return

    prompt         = active["prompt"]
    workdir        = active["workdir"]
    continue_sess  = active["continue_session"]
    resume_id      = active["resume_id"]
    perm_mode      = active["permission_mode"]
    agent_name     = active.get("agent_name", _current_agent_name(context))
    cancel_event   = active["cancel_event"]
    chat_id        = update.effective_chat.id
    task_timeout   = _get(context, "task_timeout")

    # Create the background task entry before cancelling so the ID is ready
    bg_task = task_manager.create(
        prompt=prompt,
        workdir=workdir,
        chat_id=chat_id,
        agent_name=agent_name,
    )

    asyncio_task = asyncio.create_task(
        _run_background_task(bg_task, context.bot, continue_sess, resume_id, perm_mode, timeout=task_timeout)
    )
    bg_task._asyncio_task = asyncio_task

    # Signal the subprocess to die, then cancel the asyncio wrapper task
    cancel_event.set()
    inline_task = active.get("_task")
    if inline_task and not inline_task.done():
        inline_task.cancel()

    preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
    await update.message.reply_text(
        t("bg_moved",
          task_id=bg_task.task_id,
          preview=preview,
          workdir=Path(workdir).name,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("fg", "Background tasks", "bring to foreground",
                howto="Usage: `/fg [id]`\n\nWaits for a running task and shows its output inline.\nIf id is omitted, picks the most recent running task.\nIf the task is already finished, shows its output immediately.",
                )
async def cmd_fg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bring a background task to foreground — wait for it and show output inline."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    chat_id = update.effective_chat.id

    if context.args:
        try:
            task_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                _cmd_usage("fg"), parse_mode=ParseMode.MARKDOWN,
            )
            return
        bg_task = task_manager.get(task_id)
        if bg_task is None or bg_task.chat_id != chat_id:
            await update.message.reply_text(t("fg_not_found", task_id=task_id))
            return
    else:
        running = task_manager.running_for_chat(chat_id)
        if not running:
            # Try the most recent completed task
            all_tasks = task_manager.for_chat(chat_id)
            if all_tasks and all_tasks[0].status != TaskStatus.RUNNING:
                bg_task = all_tasks[0]
            else:
                await update.message.reply_text(t("no_tasks"))
                return
        else:
            bg_task = running[0]

    # If the task is already done, show its output immediately
    if bg_task.status != TaskStatus.RUNNING:
        if bg_task.output:
            preview = bg_task.prompt[:60] + ("…" if len(bg_task.prompt) > 60 else "")
            header = t("fg_done_header",
                       task_id=bg_task.task_id,
                       elapsed=bg_task.elapsed,
                       status=bg_task.status.value,
                       preview=preview)
            await send_chunks(update, header + "\n\n" + bg_task.output)
        else:
            await update.message.reply_text(
                t("fg_no_output", task_id=bg_task.task_id, status=bg_task.status.value),
            )
        return

    # Task is still running — show a live message and wait
    preview = bg_task.prompt[:60] + ("…" if len(bg_task.prompt) > 60 else "")
    live_msg = await update.message.reply_text(
        t("fg_waiting", task_id=bg_task.task_id, preview=preview),
        parse_mode=ParseMode.MARKDOWN,
    )

    # Wait for the asyncio task to finish
    if bg_task._asyncio_task and not bg_task._asyncio_task.done():
        try:
            await asyncio.shield(bg_task._asyncio_task)
        except (asyncio.CancelledError, Exception):
            pass

    try:
        await live_msg.delete()
    except Exception:
        pass

    if bg_task.output:
        header = t("fg_done_header",
                    task_id=bg_task.task_id,
                    elapsed=bg_task.elapsed,
                    status=bg_task.status.value,
                    preview=preview)
        await send_chunks(update, header + "\n\n" + bg_task.output)
    else:
        await update.message.reply_text(
            t("fg_no_output", task_id=bg_task.task_id, status=bg_task.status.value),
        )


@_register_cmd("sendme", "Navigation & files", "send file as attachment", shortcuts=["dl"],
                howto="Usage: `/sendme <path or glob>`\n\n"
                      "Examples:\n"
                      "`/sendme report.pdf`\n"
                      "`/sendme /tmp/output.log`\n"
                      "`/sendme src/*.py` → sent as ZIP",
                )
async def cmd_sendme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send one or more files as Telegram attachment."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    if not context.args:
        await update.message.reply_text(
            _cmd_usage("sendme"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    workdir = Path(_get(context, "homedir"))
    pattern = " ".join(context.args)

    # Resolve the pattern into a list of concrete files
    candidate = Path(pattern)
    # Treat paths starting with / or drive letter as absolute
    if candidate.is_absolute() or pattern.startswith("/"):
        resolved = candidate.resolve()
        if "*" in pattern or "?" in pattern:
            base = resolved.parent
            matches = sorted(base.glob(resolved.name))
        elif resolved.is_file():
            matches = [resolved]
        elif resolved.is_dir():
            matches = sorted(resolved.iterdir())
        else:
            matches = []
    else:
        matches = sorted(workdir.glob(pattern))

    files = [p for p in matches if p.is_file()]

    if not files:
        await update.message.reply_text(
            f"❌ No files found for: `{pattern}`\n"
            f"📁 Workdir: `{_short_path(workdir)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

    if len(files) == 1:
        file_path = files[0]
        try:
            size = file_path.stat().st_size
            if size > TELEGRAM_MAX_BYTES:
                await update.message.reply_text(
                    f"❌ `{file_path.name}` is too large "
                    f"({size / 1024 / 1024:.1f} MB, limit 50 MB).",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            with open(file_path, "rb") as fh:
                await update.message.reply_document(
                    document=fh,
                    filename=file_path.name,
                )
        except Exception as exc:
            logger.exception("Error sending file %s", file_path)
            await update.message.reply_text(f"❌ Error: {exc}")

    else:
        status_msg = await update.message.reply_text(
            f"🗜 Compressing {len(files)} files into ZIP…"
        )
        zip_stem = re.sub(r"[^\w\-]", "_", pattern)  
        zip_stem = re.sub(r"_+", "_", zip_stem)        
        zip_stem = zip_stem.strip("_") or "files"      
        zip_name = zip_stem + ".zip"

        buf = io.BytesIO()
        skipped = []

        try:
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fp in files:
                    try:
                        # ZIP internal path: relative to workdir if possible
                        try:
                            arcname = fp.relative_to(workdir)
                        except ValueError:
                            arcname = fp.name
                        zf.write(fp, arcname=arcname)
                    except Exception as exc:
                        logger.warning("Skip %s: %s", fp, exc)
                        skipped.append(fp.name)

            zip_size = buf.tell()

            if zip_size > TELEGRAM_MAX_BYTES:
                await status_msg.edit_text(
                    f"❌ ZIP too large ({zip_size / 1024 / 1024:.1f} MB, limit 50 MB).\n"
                    f"Try a more selective pattern."
                )
                return

            buf.seek(0)
            caption = f"{len(files) - len(skipped)} files · {zip_size / 1024:.0f} KB"
            if skipped:
                caption += f"\n⚠️ Skipped: {', '.join(skipped)}"

            await status_msg.delete()
            await update.message.reply_document(
                document=buf,
                filename=zip_name,
                caption=caption,
            )

        except Exception as exc:
            logger.exception("Error creating ZIP")
            await status_msg.edit_text(f"❌ Error creating ZIP: {exc}")


@_register_cmd("windows", "Navigation & files", "list open windows", shortcuts=["wl"],
                howto="Usage: `/windows [filter]`\nLists visible windows with their titles. Use with `/stamp <title>`.")
async def cmd_windows(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List visible windows with their titles."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    import sys as _sys
    filter_text = " ".join(context.args).lower() if context.args else ""

    if _sys.platform == "win32":
        ps_script = (
            "Add-Type @'\n"
            "using System;\n"
            "using System.Runtime.InteropServices;\n"
            "using System.Text;\n"
            "using System.Collections.Generic;\n"
            "public class WinList {\n"
            "  [DllImport(\"user32.dll\")] static extern bool EnumWindows(EnumWindowsProc e, IntPtr l);\n"
            "  [DllImport(\"user32.dll\")] static extern int GetWindowTextLength(IntPtr h);\n"
            "  [DllImport(\"user32.dll\")] static extern int GetWindowText(IntPtr h, StringBuilder b, int m);\n"
            "  [DllImport(\"user32.dll\")] static extern bool IsWindowVisible(IntPtr h);\n"
            "  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);\n"
            "  public static List<string> Get() {\n"
            "    var r = new List<string>();\n"
            "    EnumWindows((h, l) => {\n"
            "      if (!IsWindowVisible(h)) return true;\n"
            "      int len = GetWindowTextLength(h);\n"
            "      if (len == 0) return true;\n"
            "      var sb = new StringBuilder(len + 1);\n"
            "      GetWindowText(h, sb, sb.Capacity);\n"
            "      r.Add(sb.ToString());\n"
            "      return true;\n"
            "    }, IntPtr.Zero);\n"
            "    return r;\n"
            "  }\n"
            "}\n"
            "'@\n"
            "[WinList]::Get() | ForEach-Object { $_ }"
        )
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", ps_script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        titles = [line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()]
    else:
        # Linux/macOS: use wmctrl or AppleScript
        if _sys.platform == "darwin":
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                'tell application "System Events" to get name of every window of every process whose visible is true',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-l",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if _sys.platform == "darwin":
            # AppleScript returns comma-separated
            titles = [t.strip() for t in stdout.decode(errors="replace").split(",") if t.strip()]
        else:
            # wmctrl: 4th column onwards is the title
            titles = []
            for line in stdout.decode(errors="replace").splitlines():
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    titles.append(parts[3])

    if filter_text:
        titles = [t for t in titles if filter_text in t.lower()]

    if not titles:
        await update.message.reply_text(t("no_windows_found"))
        return

    lines = [f"• `{title}`" for title in sorted(titles)]
    text = t("windows_header") + "\n\n" + "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@_register_cmd("stamp", "Navigation & files", "screenshot",
                howto="Usage: `/stamp [window title]`\nNo args = full screen. With title = that window.",
                aliases=["screenshot"])
async def cmd_stamp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture full screen or a specific window and send as photo."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    window_title = " ".join(context.args) if context.args else ""
    caption = f"🖥 {window_title}" if window_title else "🖥 Full screen"
    status_msg = await update.message.reply_text(f"📸 Capturing: {caption}…")

    screenshot_path = Path(tempfile.gettempdir()) / "tgbot_screenshot.png"

    if window_title:
        # Capture a specific window by title (substring match via EnumWindows,
        # PrintWindow API for correct DPI-independent capture)
        escaped_title = window_title.replace(chr(39), chr(39)+chr(39))
        ps_script = (
            "Add-Type @'\n"
            "using System;\n"
            "using System.Runtime.InteropServices;\n"
            "using System.Text;\n"
            "using System.Drawing;\n"
            "using System.Drawing.Imaging;\n"
            "public class WinCapture {\n"
            "  public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);\n"
            "  [DllImport(\"user32.dll\")] static extern bool EnumWindows(EnumWindowsProc cb, IntPtr l);\n"
            "  [DllImport(\"user32.dll\")] static extern int GetWindowTextLength(IntPtr h);\n"
            "  [DllImport(\"user32.dll\")] static extern int GetWindowText(IntPtr h, StringBuilder b, int m);\n"
            "  [DllImport(\"user32.dll\")] static extern bool IsWindowVisible(IntPtr h);\n"
            "  [DllImport(\"user32.dll\")] static extern bool GetWindowRect(IntPtr h, out RECT r);\n"
            "  [DllImport(\"user32.dll\")] static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);\n"
            "  [DllImport(\"user32.dll\")] static extern bool SetForegroundWindow(IntPtr h);\n"
            "  [DllImport(\"dwmapi.dll\")] static extern int DwmGetWindowAttribute(IntPtr h, int a, out RECT r, int s);\n"
            "  [DllImport(\"user32.dll\")] static extern bool SetProcessDPIAware();\n"
            "  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }\n"
            "  static IntPtr FindBySubstring(string sub) {\n"
            "    IntPtr found = IntPtr.Zero;\n"
            "    string lower = sub.ToLower();\n"
            "    EnumWindows((h, l) => {\n"
            "      if (!IsWindowVisible(h)) return true;\n"
            "      int len = GetWindowTextLength(h);\n"
            "      if (len == 0) return true;\n"
            "      var sb = new StringBuilder(len + 1);\n"
            "      GetWindowText(h, sb, sb.Capacity);\n"
            "      if (sb.ToString().ToLower().Contains(lower)) { found = h; return false; }\n"
            "      return true;\n"
            "    }, IntPtr.Zero);\n"
            "    return found;\n"
            "  }\n"
            "  public static void Capture(string title, string path) {\n"
            "    SetProcessDPIAware();\n"
            "    IntPtr h = FindBySubstring(title);\n"
            "    if (h == IntPtr.Zero) throw new Exception(\"Window not found: \" + title);\n"
            "    RECT r;\n"
            "    // DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS=9) gives true pixel rect\n"
            "    if (DwmGetWindowAttribute(h, 9, out r, Marshal.SizeOf(typeof(RECT))) != 0)\n"
            "      GetWindowRect(h, out r);\n"
            "    int w = r.R - r.L, ht = r.B - r.T;\n"
            "    if (w <= 0 || ht <= 0) throw new Exception(\"Invalid window rect: \" + w + \"x\" + ht);\n"
            "    var bmp = new Bitmap(w, ht);\n"
            "    using (var g = Graphics.FromImage(bmp)) {\n"
            "      IntPtr hdc = g.GetHdc();\n"
            "      // PW_RENDERFULLCONTENT = 2: captures even off-screen / occluded content\n"
            "      if (!PrintWindow(h, hdc, 2)) {\n"
            "        g.ReleaseHdc(hdc);\n"
            "        // Fallback to CopyFromScreen\n"
            "        g.CopyFromScreen(r.L, r.T, 0, 0, new Size(w, ht));\n"
            "      } else {\n"
            "        g.ReleaseHdc(hdc);\n"
            "      }\n"
            "    }\n"
            "    bmp.Save(path, ImageFormat.Png); bmp.Dispose();\n"
            "  }\n"
            "}\n"
            "'@ -ReferencedAssemblies System.Drawing;\n"
            f"[WinCapture]::Capture('{escaped_title}', '{screenshot_path}')"
        )
    else:
        # Primary monitor only (DPI-aware)
        ps_script = (
            "Add-Type @'\n"
            "using System;\n"
            "using System.Runtime.InteropServices;\n"
            "using System.Drawing;\n"
            "using System.Drawing.Imaging;\n"
            "public class ScreenCapture {\n"
            "  [DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();\n"
            "  [DllImport(\"user32.dll\")] public static extern int GetSystemMetrics(int i);\n"
            "  public static void Capture(string path) {\n"
            "    SetProcessDPIAware();\n"
            "    int w = GetSystemMetrics(0), h = GetSystemMetrics(1);\n"
            "    if (w <= 0 || h <= 0) throw new Exception(\"Cannot detect screen dimensions (\" + w + \"x\" + h + \")\");\n"
            "    var bmp = new Bitmap(w, h);\n"
            "    using (var g = Graphics.FromImage(bmp)) {\n"
            "      g.CopyFromScreen(0, 0, 0, 0, new Size(w, h));\n"
            "    }\n"
            "    bmp.Save(path, ImageFormat.Png); bmp.Dispose();\n"
            "  }\n"
            "}\n"
            f"'@ -ReferencedAssemblies System.Drawing;\n"
            f"[ScreenCapture]::Capture('{screenshot_path}')"
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", ps_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            await status_msg.edit_text(f"❌ Screenshot failed:\n`{err[:500]}`", parse_mode=ParseMode.MARKDOWN)
            return

        if not screenshot_path.is_file():
            await status_msg.edit_text(t("screenshot_not_created"))
            return

        # Telegram rejects photos larger than ~10000px on any side or ~10MP total.
        # Resize if needed, otherwise send as document for huge captures.
        from PIL import Image as PILImage
        img = PILImage.open(screenshot_path)
        w, h = img.size
        max_side = 10000
        max_pixels = 10_000_000  # ~10MP
        if w * h > max_pixels or max(w, h) > max_side:
            scale = min(max_side / max(w, h), (max_pixels / (w * h)) ** 0.5)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), PILImage.LANCZOS)
            img.save(screenshot_path, "PNG")
            caption += f" (resized {w}x{h} -> {new_w}x{new_h})"
        img.close()

        with open(screenshot_path, "rb") as fh:
            await update.message.reply_photo(photo=fh, caption=caption)

        await status_msg.delete()

    except asyncio.TimeoutError:
        await status_msg.edit_text(t("screenshot_timeout"))
    except Exception as exc:
        logger.exception("Error capturing screenshot")
        await status_msg.edit_text(f"❌ Error: {exc}")
    finally:
        if screenshot_path.is_file():
            screenshot_path.unlink(missing_ok=True)


async def _detect_dshow_devices() -> tuple[str, str]:
    """Auto-detect DirectShow video and audio device names via ffmpeg."""
    video_dev = config.WEBCAM_VIDEO_DEVICE
    audio_dev = config.WEBCAM_AUDIO_DEVICE
    if video_dev and audio_dev:
        return video_dev, audio_dev
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, out = await asyncio.wait_for(proc.communicate(), timeout=10)
    for line in out.decode(errors="replace").splitlines():
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        if "(video)" in line and not video_dev:
            video_dev = m.group(1)
        elif "(audio)" in line and not audio_dev:
            audio_dev = m.group(1)
    return video_dev, audio_dev


async def _ffmpeg_run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run an ffmpeg command and return (returncode, stderr_text)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, stderr.decode(errors="replace").strip()


async def _probe_video(path: Path, default_duration: int) -> tuple[int, int, int]:
    """Probe an MP4 for width, height, duration. Returns defaults on failure."""
    vid_w, vid_h, vid_dur = 640, 480, default_duration
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(out.decode())
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                vid_w = int(s.get("width", vid_w))
                vid_h = int(s.get("height", vid_h))
                break
        vid_dur = int(float(data.get("format", {}).get("duration", default_duration)))
    except Exception:
        pass
    return vid_w, vid_h, vid_dur


@_register_cmd("webcam", "Navigation & files", "record 3 s video from webcam", shortcuts=["cam"],
                howto="Usage: `/webcam [seconds]`\nRecords video+audio from the webcam (default 3 s, max 30 s).",
                aliases=["cam"])
async def cmd_webcam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record a short video clip from the webcam with audio and send it."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    duration = 3
    if context.args:
        try:
            duration = max(1, min(30, int(context.args[0])))
        except ValueError:
            await update.message.reply_text(t("duration_must_be_number"))
            return

    status_msg = await update.message.reply_text(t("recording", duration=duration))
    warmup = 2
    video_path = Path(tempfile.gettempdir()) / "tgbot_webcam.mp4"
    raw_path = Path(tempfile.gettempdir()) / "tgbot_webcam_raw.avi"

    try:
        video_dev, audio_dev = await _detect_dshow_devices()
        if not video_dev:
            await status_msg.edit_text(t("no_video_device"))
            return

        dshow_input = f"video={video_dev}"
        if audio_dev:
            dshow_input += f":audio={audio_dev}"

        # Pass 1: raw capture (native MJPEG passthrough + PCM audio)
        capture_cmd = [
            "ffmpeg", "-y", "-f", "dshow",
            "-video_size", config.WEBCAM_RESOLUTION,
            "-framerate", str(config.WEBCAM_FRAMERATE),
            "-rtbufsize", "200M",
            "-i", dshow_input,
            "-t", str(warmup + duration),
            "-c:v", "copy",
        ]
        if audio_dev:
            capture_cmd += ["-c:a", "pcm_s16le"]
        capture_cmd.append(str(raw_path))

        rc, err = await _ffmpeg_run(capture_cmd, timeout=warmup + duration + 15)
        if rc != 0:
            await status_msg.edit_text(f"❌ Recording failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
            return

        # Pass 2: trim warm-up, encode H264+AAC for Telegram
        encode_cmd = [
            "ffmpeg", "-y", "-ss", str(warmup), "-i", str(raw_path),
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(video_path),
        ]
        rc, err = await _ffmpeg_run(encode_cmd, timeout=30)
        if rc != 0:
            await status_msg.edit_text(f"❌ Encoding failed:\n`{err[-500:]}`", parse_mode=ParseMode.MARKDOWN)
            return

        if not video_path.is_file() or video_path.stat().st_size == 0:
            await status_msg.edit_text(t("video_not_created"))
            return

        vid_w, vid_h, vid_dur = await _probe_video(video_path, duration)
        with open(video_path, "rb") as fh:
            await update.message.reply_video(
                video=fh, caption=t("webcam_caption", duration=duration),
                width=vid_w, height=vid_h, duration=vid_dur,
                supports_streaming=True,
            )
        await status_msg.delete()

    except asyncio.TimeoutError:
        await status_msg.edit_text(f"❌ Recording timed out ({duration + 15}s).")
    except Exception as exc:
        logger.exception("Error recording webcam")
        await status_msg.edit_text(f"❌ Error: {exc}")
    finally:
        for f in (video_path, raw_path):
            if f.is_file():
                f.unlink(missing_ok=True)


@_register_cmd("settings", "Settings & info", "current settings", shortcuts=["env"])
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows current settings panel."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return
    await update.message.reply_text(_settings_panel(context), parse_mode=ParseMode.MARKDOWN)


@_register_cmd("agent", "Settings & info", "show or change CLI agent", shortcuts=["ag"],
                howto="Usage: `/agent [claude|codex|copilot]`",
                )
async def cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    current = _current_agent_name(context)
    if not context.args:
        options = ", ".join(f"`{name}`" for name in _AGENT_CHOICES)
        await update.message.reply_text(
            f"🤖 Current agent: `{current}` ({_current_agent_label(context)})\n\n"
            f"Available: {options}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    selected = context.args[0].lower()
    if selected not in _AGENT_CHOICES:
        options = ", ".join(f"`{name}`" for name in _AGENT_CHOICES)
        await update.message.reply_text(
            f"❌ Accepted values for `agent`: {options}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data["setting_agent"] = selected
    context.user_data["continue_session"] = False
    context.user_data.pop("resume_id", None)
    await update.message.reply_text(
        f"✅ `agent` set to `{selected}` ({_agent_for_name(selected).display_name})\n"
        f"Session state cleared for the new agent.",
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("set", "Settings & info", "change a setting",
                howto="Usage: `/set <param> <value>`",
                )
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a parameter."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    if not context.args or len(context.args) < 2:
        params = ", ".join(f"`{k}`" for k in SETTINGS_SCHEMA)
        await update.message.reply_text(
            _cmd_usage("set") + "\n\n"
            f"Available params: {params}\n\n"
            "Current values: /settings",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    key = context.args[0].lower()
    raw_value = " ".join(context.args[1:])

    if key not in SETTINGS_SCHEMA:
        params = ", ".join(f"`{k}`" for k in SETTINGS_SCHEMA)
        await update.message.reply_text(
            f"❌ Unknown param: `{key}`\n\nAvailable: {params}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    schema = SETTINGS_SCHEMA[key]

    # Reset to default
    if raw_value.lower() == "reset":
        context.user_data.pop(f"setting_{key}", None)
        if key == "agent":
            context.user_data["continue_session"] = False
            context.user_data.pop("resume_id", None)
        default = schema["default"]
        default_val = default() if callable(default) else default
        await update.message.reply_text(
            f"↩️ `{key}` reset to default: `{default_val}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Validation and type conversion
    try:
        if schema["type"] == "path":
            parsed, err = _resolve_workdir(raw_value)
            if err:
                await update.message.reply_text(f"❌ {err}", parse_mode=ParseMode.MARKDOWN)
                return
            context.user_data["homedir"] = parsed

        elif schema["type"] == "float":
            parsed = float(raw_value.replace(",", "."))
            lo, hi = schema["min"], schema["max"]
            if not (lo <= parsed <= hi):
                await update.message.reply_text(
                    f"❌ `{key}` must be between {lo} and {hi}.", parse_mode=ParseMode.MARKDOWN
                )
                return

        elif schema["type"] == "int":
            parsed = int(raw_value)
            lo, hi = schema["min"], schema["max"]
            if not (lo <= parsed <= hi):
                await update.message.reply_text(
                    f"❌ `{key}` must be between {lo} and {hi}.", parse_mode=ParseMode.MARKDOWN
                )
                return

        elif schema["type"] == "choice":
            parsed = raw_value.lower()
            choices = schema["choices"]
            if parsed not in choices:
                opts = ", ".join(f"`{c}`" for c in choices)
                await update.message.reply_text(
                    f"❌ Accepted values for `{key}`: {opts}", parse_mode=ParseMode.MARKDOWN
                )
                return
            # Sync with transcriber config only for language setting
            if key == "language":
                config.WHISPER_LANGUAGE = parsed
            elif key == "agent":
                context.user_data["continue_session"] = False
                context.user_data.pop("resume_id", None)

    except ValueError:
        await update.message.reply_text(
            f"❌ Invalid value for `{key}`: `{raw_value}`\n"
            f"Example: `/set {key} {schema['example']}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data[f"setting_{key}"] = parsed
    await update.message.reply_text(
        f"✅ `{key}` set to `{parsed}`", parse_mode=ParseMode.MARKDOWN
    )


@_register_cmd("ls", "Navigation & files", "list files",
                howto="Usage: `/ls [opts] [path/glob]`\nOptions: -t time, -r reverse, -N limit",
                aliases=["dir"])
async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    workdir = _get(context, "homedir")
    args = list(context.args) if context.args else []

    # Parse options
    sort_by_time = False
    reverse = False
    limit = 10
    pattern = None

    remaining = []
    for a in args:
        if a == "-t":
            sort_by_time = True
        elif a == "-r":
            reverse = True
        elif re.match(r"^-(\d+)$", a):
            limit = int(a[1:])
        else:
            remaining.append(a)

    # If remaining arg, it's the path/pattern
    if remaining:
        raw = " ".join(remaining)
        is_abs = Path(raw).is_absolute() or raw.startswith("/")
        if "*" in raw or "?" in raw:
            p = Path(raw)
            if not is_abs:
                p = Path(workdir) / p
            parent = p.resolve().parent
            pattern = p.name
            workdir = str(parent)
        else:
            p = Path(raw)
            if not is_abs:
                p = Path(workdir) / p
            workdir = str(p.resolve())

    base = Path(workdir)
    if not base.is_dir():
        await update.message.reply_text(f"❌ Directory not found: `{_short_path(workdir)}`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        entries = list(base.iterdir())
    except PermissionError:
        await update.message.reply_text(f"❌ Permission denied: `{_short_path(workdir)}`", parse_mode=ParseMode.MARKDOWN)
        return

    # Filter by pattern
    if pattern:
        entries = [e for e in entries if fnmatch.fnmatch(e.name, pattern)]

    # Sort
    if sort_by_time:
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=not reverse)
    else:
        entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()), reverse=reverse)

    total = len(entries)
    show = entries[:limit] if limit > 0 else entries

    lines = []
    for e in show:
        icon = "📁" if e.is_dir() else "📄"
        lines.append(f"{icon} `{e.name}`")

    listing = "\n".join(lines) if lines else "_(empty)_"
    more = f"\n\n_…and {total - limit} more_" if total > limit > 0 else ""
    pat_info = f" (`{pattern}`)" if pattern else ""

    await update.message.reply_text(
        f"📁 `{_short_path(workdir)}`{pat_info}\n\n{listing}{more}",
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("cd", "Navigation & files", "change directory",
                howto="Usage: `/cd <path>`",
                )
async def cmd_cd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return
    if not context.args:
        workdir = _get(context, "homedir")
        await update.message.reply_text(t("dir_current", path=_short_path(workdir)), parse_mode=ParseMode.MARKDOWN)
        return
    raw = " ".join(context.args)
    resolved, err = _resolve_workdir(raw)
    if resolved is None:
        await update.message.reply_text(f"❌ {err}", parse_mode=ParseMode.MARKDOWN)
        return
    # Update both homedir and setting_homedir for consistency
    context.user_data["homedir"] = resolved
    context.user_data["setting_homedir"] = resolved
    context.user_data["continue_session"] = False
    await update.message.reply_text(t("dir_changed", path=_short_path(resolved)), parse_mode=ParseMode.MARKDOWN)


async def _cmd_head_tail(update: Update, context: ContextTypes.DEFAULT_TYPE, from_end: bool) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    cmd_name = "tail" if from_end else "head"

    if not context.args:
        await update.message.reply_text(_cmd_usage(cmd_name), parse_mode=ParseMode.MARKDOWN)
        return

    # Parse -N option (Unix-style) and trailing N (legacy)
    args = list(context.args)
    n = 10
    remaining = []
    for a in args:
        m = re.match(r"^-(\d+)$", a)
        if m:
            n = max(1, min(int(m.group(1)), 200))
        else:
            remaining.append(a)
    # Legacy: trailing bare number
    if remaining and remaining[-1].isdigit():
        n = max(1, min(int(remaining.pop()), 200))

    raw_path = " ".join(remaining)
    workdir = _get(context, "homedir")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(workdir) / path
    path = path.resolve()

    if not path.is_file():
        await update.message.reply_text(f"❌ File not found: `{_short_path(path)}`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as exc:
        await update.message.reply_text(f"❌ Read error: {exc}")
        return

    selected = lines[-n:] if from_end else lines[:n]
    content = "".join(selected)

    header = f"{'🔚' if from_end else '🔝'} `{path.name}` — {'last' if from_end else 'first'} {len(selected)} lines of {len(lines)}\n\n"
    body = f"```\n{content.rstrip()}\n```"

    # Telegram has 4096 char limit
    full = header + body
    if len(full) > 4096:
        full = header + f"```\n{content[-(4096 - len(header) - 10):].rstrip()}\n```"

    await update.message.reply_text(full, parse_mode=ParseMode.MARKDOWN)


@_register_cmd("head", "Navigation & files", "first n lines",
                howto="Usage: `/head [-N] <file>`\nDefault N=10. Example: `/head -20 app.log`",
                )
async def cmd_head(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_head_tail(update, context, from_end=False)


@_register_cmd("tail", "Navigation & files", "last n lines",
                howto="Usage: `/tail [-N] <file>`\nDefault N=10. Example: `/tail -20 app.log`",
                )
async def cmd_tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_head_tail(update, context, from_end=True)


@_register_cmd("usage", "Settings & info", "token usage", shortcuts=["u"])
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    agent = _current_agent(context)

    if not agent.capabilities.usage:
        await update.message.reply_text(
            f"ℹ️ Usage details are not available for {agent.display_name}.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not _last_usage or _last_usage.get("agent_name") != agent.name:
        await update.message.reply_text(t("no_usage_data"))
        return

    usage = _last_usage.get("usage", {})
    input_tok = usage.get("input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cost = _last_usage.get("total_cost_usd", 0)
    turns = _last_usage.get("num_turns", 0)
    dur = _last_usage.get("duration_ms", 0)
    dur_api = _last_usage.get("duration_api_ms", 0)
    session_id = _last_usage.get("session_id", "?")

    # Model usage breakdown
    model_usage = _last_usage.get("model_usage", {})
    model_lines = ""
    for model, mu in model_usage.items():
        m_in = mu.get("inputTokens", 0)
        m_out = mu.get("outputTokens", 0)
        m_cache_r = mu.get("cacheReadInputTokens", 0)
        m_cache_w = mu.get("cacheCreationInputTokens", 0)
        model_lines += f"\n  `{model}`\n  in={m_in:,} out={m_out:,} cache_r={m_cache_r:,} cache_w={m_cache_w:,}"

    text = (
        f"📊 *Last response usage*\n\n"
        f"• Input tokens: `{input_tok:,}`\n"
        f"• Output tokens: `{output_tok:,}`\n"
        f"• Cache create: `{cache_create:,}`\n"
        f"• Cache read: `{cache_read:,}`\n"
        f"• Cost: `${cost:.4f}`\n"
        f"• Turns: `{turns}`\n"
        f"• Duration: `{dur / 1000:.1f}s` (API: `{dur_api / 1000:.1f}s`)\n"
        f"• Session: `{session_id[:8]}`"
    )
    if model_lines:
        text += f"\n\n*Per model:*{model_lines}"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


@_register_cmd("history", "Settings & info", "show recent commands",
                howto="Usage: `/history [n]`\nDefault n=5. Shows the last n commands/prompts sent.\n`!N` re-executes the Nth entry (1 = most recent).",
                shortcuts=["hist"],
                aliases=["!"],
                )
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    history = _get_history(context)
    if not history:
        await update.message.reply_text(t("history_empty"))
        return

    n = 5
    if context.args:
        try:
            n = max(1, min(int(context.args[0]), _HISTORY_MAX))
        except ValueError:
            pass

    items = list(history)[-n:]
    items.reverse()  # newest first, matching !N numbering
    lines = [t("history_header", n=len(items))]
    for i, entry in enumerate(items, 1):
        preview = entry[:80] + ("…" if len(entry) > 80 else "")
        lines.append(f"`!{i}` {preview}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@_register_cmd("last", "Settings & info", "last prompt and response", shortcuts=["ll"])
async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    agent_name = _current_agent_name(context)
    prompt = _last_prompt if _last_agent_name == agent_name else ""
    response = _last_response if _last_agent_name == agent_name else ""

    # If no in-memory data, ask the active session backend.
    if not prompt and not response:
        workdir = _get(context, "homedir")
        session_id = _last_usage.get("session_id", "") if _last_usage.get("agent_name") == agent_name else ""
        prompt, response = read_last_interaction(workdir, session_id=session_id, backend=agent_name)

    if not prompt and not response:
        await update.message.reply_text(t("no_previous_interaction"))
        return

    prompt_preview = prompt[:300] + ("…" if len(prompt) > 300 else "")
    resp_lines = [l for l in response.splitlines() if l.strip()]
    last_line = resp_lines[-1].strip() if resp_lines else "(empty)"
    last_line_preview = last_line[:500] + ("…" if len(last_line) > 500 else "")

    text = (
        f"📝 Last interaction\n\n"
        f"Prompt:\n{prompt_preview}\n\n"
        f"Response ({len(response):,} chars, {len(resp_lines)} lines):\n"
        f"…{last_line_preview}"
    )
    await update.message.reply_text(text)


@_register_cmd("read", "Settings & info", "read last response as voice", shortcuts=["rd"])
async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    agent_name = _current_agent_name(context)
    response = _last_response if _last_agent_name == agent_name else ""

    # If no in-memory data, ask the active session backend.
    if not response:
        workdir = _get(context, "homedir")
        session_id = _last_usage.get("session_id", "") if _last_usage.get("agent_name") == agent_name else ""
        _, response = read_last_interaction(workdir, session_id=session_id, backend=agent_name)

    if not response:
        await update.message.reply_text(t("no_previous_interaction"))
        return

    await update.message.reply_text(t("read_tts_generating"))
    try:
        audio_path = await asyncio.get_running_loop().run_in_executor(
            None, tts_synthesize, response
        )
        with open(audio_path, "rb") as af:
            await update.message.reply_voice(voice=af)
        os.unlink(audio_path)
    except Exception as exc:
        logger.warning("TTS failed for /read: %s", exc)
        await update.message.reply_text(t("error_generic", error=str(exc)))


@_register_cmd("status", "Settings & info", "session and task info", shortcuts=["st"])
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_active = context.user_data.get("continue_session", False)
    resume_id = context.user_data.get("resume_id")
    session_title = context.user_data.get("session_title", "")
    has_session = session_active or resume_id
    session_info = "✅ yes"
    if resume_id:
        session_info += f" (resume `{resume_id[:8]}`)"
    elif session_active:
        session_info += " (continue)"
    if session_title:
        session_info += f" — _{session_title}_"

    chat_id = update.effective_chat.id
    running = task_manager.running_for_chat(chat_id)
    running_str = ", ".join(f"#{t.task_id}" for t in running) if running else "none"
    agent_label = _current_agent_label(context)

    workdir = _get(context, "homedir")

    await update.message.reply_text(
        f"📊 *Status*\n"
        f"• Agent: `{agent_label}`\n"
        f"• Session: {session_info if has_session else '❌ no'}\n"
        f"• Tasks: {running_str}\n"
        f"• Dir: `{_short_path(workdir)}`",
        parse_mode=ParseMode.MARKDOWN,
    )


@_register_cmd("shutdown", "Settings & info", "stop bot and allow PC standby", shortcuts=["halt"],
                howto="Usage: `/shutdown [seconds]`\n"
                      "Stops bot + watchdog so the PC can sleep.\n"
                      "`/shutdown 1800` = wake + restart after 30 min.",
                )
async def cmd_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    restart_seconds = 0
    if context.args:
        # Accept: "/shutdown 1800", "/shutdown -restart 1800" (legacy)
        args = list(context.args)
        for i, a in enumerate(args):
            if a == "-restart" and i + 1 < len(args):
                try:
                    restart_seconds = max(60, int(args[i + 1]))
                except ValueError:
                    await update.message.reply_text(t("restart_needs_number"))
                    return
                break
            try:
                restart_seconds = max(60, int(a))
                break
            except ValueError:
                continue

    if restart_seconds:
        await _schedule_wake_restart(restart_seconds)
        mins = restart_seconds // 60
        await update.message.reply_text(
            t("shutdown_restart", mins=mins, seconds=restart_seconds)
        )
    else:
        await update.message.reply_text(t("shutdown_msg"))

    # Exit code 42 tells the watchdog NOT to restart
    os._exit(42)


async def _schedule_wake_restart(seconds: int) -> None:
    """Schedule a delayed restart. On Windows, uses a wake timer to wake from standby.
    On Linux/macOS, uses systemd-run or launchd (best-effort, no wake from sleep)."""
    import sys as _sys
    from vibeaway.paths import RUNTIME_DIR

    venv_dir = RUNTIME_DIR / "venv"
    watchdog = RUNTIME_DIR / "service" / "bot_watchdog.pyw"

    if _sys.platform == "win32":
        await _schedule_wake_windows(seconds, venv_dir, watchdog, RUNTIME_DIR)
    elif _sys.platform == "darwin":
        await _schedule_wake_macos(seconds, venv_dir, watchdog)
    else:
        await _schedule_wake_linux(seconds, venv_dir, watchdog)


async def _schedule_wake_windows(seconds: int, venv_dir: Path, watchdog: Path, runtime_dir: Path) -> None:
    pythonw = venv_dir / "Scripts" / "pythonw.exe"
    task_name = "VibeAwayWake"
    # EndBoundary is required when using -DeleteExpiredTaskAfter
    # Set it to fire time + 5 minutes so the task auto-deletes after execution
    ps_script = (
        f"$taskName = '{task_name}';\n"
        f"Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue;\n"
        f"$action = New-ScheduledTaskAction -Execute '{pythonw}' "
        f"-Argument '\"{watchdog}\"' -WorkingDirectory '{runtime_dir / 'service'}';\n"
        f"$fireAt = (Get-Date).AddSeconds({seconds});\n"
        f"$endAt = $fireAt.AddMinutes(5);\n"
        f"$trigger = New-ScheduledTaskTrigger -Once -At $fireAt;\n"
        f"$trigger.EndBoundary = $endAt.ToUniversalTime().ToString('s') + 'Z';\n"
        f"$settings = New-ScheduledTaskSettingsSet "
        f"-WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        f"-ExecutionTimeLimit ([TimeSpan]::Zero) -DeleteExpiredTaskAfter (New-TimeSpan -Minutes 5);\n"
        f"Register-ScheduledTask -TaskName $taskName -Action $action "
        f"-Trigger $trigger -Settings $settings "
        f"-Description 'One-shot: wake PC and restart Telegram bot' -Force;\n"
    )
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-Command", ps_script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        logger.warning("Failed to schedule wake task: %s", stderr.decode(errors="replace")[:300])


async def _schedule_wake_linux(seconds: int, venv_dir: Path, watchdog: Path) -> None:
    python = venv_dir / "bin" / "python"
    # systemd-run --user creates a transient timer (no wake from suspend, but restarts after resume)
    proc = await asyncio.create_subprocess_exec(
        "systemd-run", "--user", "--on-active", f"{seconds}s",
        "--unit", "vibeaway-restart",
        "--description", "Delayed restart of VibeAway bot",
        str(python), str(watchdog),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        logger.warning("Failed to schedule restart via systemd-run: %s",
                        stderr.decode(errors="replace")[:300])


async def _schedule_wake_macos(seconds: int, venv_dir: Path, watchdog: Path) -> None:
    python = venv_dir / "bin" / "python"
    # Use 'at' command for delayed execution (no wake from sleep)
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c",
        f"echo '{python} {watchdog}' | at now + {seconds // 60} minutes 2>&1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        logger.warning("Failed to schedule restart via at: %s",
                        stderr.decode(errors="replace")[:300])


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmd = update.message.text.split()[0] if update.message.text else "?"
    await update.message.reply_text(
        t("unknown_command", cmd=cmd),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _exec_segment(update: Update, context: ContextTypes.DEFAULT_TYPE, segment: str) -> None:
    """Execute a single segment: dot-command or plain prompt.

    Dot-prefix rules:
      - ``.command`` is equivalent to ``/command`` (no space between dot and name)
      - ``..`` or ``. foo`` are treated as plain prompts (not commands)
    """
    if segment.startswith(".") and len(segment) > 1 and not segment[1].isspace() and not segment.startswith(".."):
        parts = segment[1:].split(None, 1)
        cmd_name = parts[0].lower()
        handler = _VOICE_CMD_MAP.get(cmd_name)
        if handler:
            context.args = parts[1].split() if len(parts) > 1 else []
            await handler(update, context)
            return
    await _process_prompt(update, context, segment)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    text = update.message.text
    logger.info("Text from user_id=%d: %s", update.effective_user.id, text[:80])

    # If awaiting modified text after safety check, use it as prompt
    if context.user_data.pop(_MODIFY_KEY, None):
        logger.info("Modified safety text: %s", text[:80])
        await _process_prompt(update, context, text)
        return

    # History shortcuts: "." = repeat last, "!N" = re-execute Nth (1-based, most recent first)
    history = _get_history(context)
    stripped = text.strip()
    if stripped == ".":
        if not history:
            await update.message.reply_text(t("history_empty"))
            return
        text = history[-1]
        logger.info("Repeating last input: %s", text[:80])
    elif stripped == "!":
        context.args = []
        await cmd_history(update, context)
        return
    elif re.fullmatch(r"!(\d+)", stripped):
        n = int(stripped[1:])
        items = list(history)
        if n < 1 or n > len(items):
            await update.message.reply_text(f"❌ `!{n}`: only {len(items)} entries in history.")
            return
        text = items[-n]
        logger.info("Re-executing !%d: %s", n, text[:80])
    else:
        history.append(text)

    # Command chaining: split on " .+ " and execute sequentially
    if " .+ " in text:
        segments = [s.strip() for s in text.split(" .+ ") if s.strip()]
        logger.info("Command chaining: %d segments", len(segments))
        for seg in segments:
            await _exec_segment(update, context, seg)
        return

    # Accept '.' as command prefix (e.g. ".ls" → "/ls")
    await _exec_segment(update, context, text)


_VOICE_CMD_MAP: dict[str, Callable] = {}
"""Populated in main() with available voice commands."""


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text(t("unauthorized"))
        return

    logger.info("Voice from user_id=%d (duration=%ds)", update.effective_user.id, update.message.voice.duration)
    status_msg = await update.message.reply_text(t("transcribing"))
    tg_file = await context.bot.get_file(update.message.voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        transcript = await asyncio.get_running_loop().run_in_executor(
            None, transcribe_audio, tmp_path
        )
    except Exception as exc:
        await status_msg.edit_text(f"❌ Transcription error: {exc}")
        return
    finally:
        os.unlink(tmp_path)

    transcript = transcript.strip()
    logger.info("Transcription complete: %s", transcript[:100])

    words = transcript.split()
    first = words[0].lower().rstrip(".,!?") if words else ""

    if first in _TRIGGER_WORDS and len(words) >= 2:
        cmd_name = words[1].lower().rstrip(".,!?")
        args = words[2:]
        logger.info("Voice command: /%s args=%s", cmd_name, args)
        await status_msg.edit_text(
            f"🎤 _{transcript}_\n→ /{cmd_name} {' '.join(args)}".rstrip(),
            parse_mode=ParseMode.MARKDOWN,
        )

        handler = _VOICE_CMD_MAP.get(cmd_name)
        if handler:
            context.args = args
            await handler(update, context)
        else:
            await update.message.reply_text(
                t("unknown_command", cmd=f"/{cmd_name}"),
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        # Safety check on voice prompt
        risks = check_safety(transcript)
        if risks:
            logger.warning("Safety check: risks=%s, user_id=%d", risks, update.effective_user.id)
            risk_list = ", ".join(f"*{r}*" for r in risks)
            context.user_data[_PENDING_KEY] = {"transcript": transcript, "voice": True}
            await update.message.reply_text(
                t("safety_warning", risks=risk_list, text=transcript),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_confirmation_keyboard(),
            )
            return

        # Direct prompt to the current CLI agent
        await status_msg.edit_text(
            f"🎤 _{transcript}_\n→ 💬 Prompt",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _process_prompt(update, context, transcript, reply_voice=True)


async def _process_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str,
    reply_voice: bool = False,
) -> None:
    """Inline execution with streaming JSON. The live message is updated progressively.

    The heavy CLI call runs as a detached asyncio.Task so the bot remains
    responsive to other commands (e.g. /bg) while the current agent is processing.
    """
    agent_name = _current_agent_name(context)
    agent = _current_agent(context)
    live_msg = await update.message.reply_text(f"⏳ {agent.display_name} is processing…")

    workdir    = _get(context, "homedir")
    timeout    = _get(context, "timeout")
    stream_iv  = _get(context, "stream_interval")
    perm_mode  = _effective_permission_mode(context)
    resume_id  = context.user_data.get("resume_id")
    continue_session = context.user_data.get("continue_session", False)
    logger.info(
        "Prompt -> %s (workdir=%s, continue=%s, resume=%s, timeout=%d, perm=%s)",
        agent.display_name, workdir, continue_session, resume_id, timeout, perm_mode,
    )

    cancel_event = asyncio.Event()
    proc_holder: list = []

    async def on_update(partial: str) -> None:
        preview = partial[:4000] + ("\n…" if len(partial) > 4000 else "")
        try:
            await live_msg.edit_text("🔄 " + preview + " ▌")
        except Exception:
            pass

    async def _run() -> None:
        global _last_agent_name, _last_prompt, _last_response
        try:
            response = await run_agent_streaming(
                agent_name,
                prompt,
                workdir=workdir,
                continue_session=continue_session,
                resume_id=resume_id,
                on_update=on_update,
                stream_interval=stream_iv,
                timeout=timeout,
                permission_mode=perm_mode,
                cancel_event=cancel_event,
                proc_holder=proc_holder,
            )
        except asyncio.CancelledError:
            # /bg moved the execution to background — drop the inline reply silently
            context.user_data.pop("active_inline", None)
            try:
                await live_msg.delete()
            except Exception:
                pass
            return
        finally:
            context.user_data.pop("active_inline", None)

        _last_agent_name = agent_name
        _last_prompt = prompt
        _last_response = response

        if resume_id:
            context.user_data.pop("resume_id", None)
        context.user_data["continue_session"] = True
        logger.info("%s response received (%d chars):\n%s", agent.display_name, len(response), response)

        try:
            await live_msg.delete()
        except Exception:
            pass
        await send_chunks(update, response)

        if reply_voice and response and not response.startswith("❌"):
            try:
                audio_path = await asyncio.get_running_loop().run_in_executor(
                    None, tts_synthesize, response
                )
                with open(audio_path, "rb") as af:
                    await update.message.reply_voice(voice=af)
                os.unlink(audio_path)
            except Exception as exc:
                logger.warning("TTS failed: %s", exc)

    # Launch as a detached task so the handler returns immediately and the bot
    # can receive /bg (or any other command) while the agent is still running.
    inline_task = asyncio.create_task(_run())

    # Expose execution state for /bg — must be set BEFORE this handler returns
    context.user_data["active_inline"] = {
        "agent_name": agent_name,
        "prompt": prompt,
        "workdir": workdir,
        "continue_session": continue_session,
        "resume_id": resume_id,
        "permission_mode": perm_mode,
        "cancel_event": cancel_event,
        "proc_holder": proc_holder,
        "_task": inline_task,
    }


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("Starting bot (workdir=%s, transcriber=%s, lang=%s)",
                 config.WORKDIR, config.TRANSCRIBER, config.WHISPER_LANGUAGE)

    # Load locale-specific aliases and trigger words
    _load_locale()

    # Pre-load Whisper model so the first voice message is fast
    if config.TRANSCRIBER == "faster_whisper":
        try:
            from vibeaway.transcriber import _get_faster_whisper_model
            _get_faster_whisper_model()
        except Exception as exc:
            logger.warning("Could not pre-load Whisper model: %s", exc)

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Register all commands, shortcuts, and voice aliases from the central registry
    for cmd in _CMD_REGISTRY:
        app.add_handler(CommandHandler(cmd.name, cmd.handler))
        _VOICE_CMD_MAP[cmd.name] = cmd.handler
        for sc in cmd.shortcuts:
            app.add_handler(CommandHandler(sc, cmd.handler))
            _VOICE_CMD_MAP[sc] = cmd.handler
        for alias in cmd.aliases:
            _VOICE_CMD_MAP[alias] = cmd.handler

    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_safety_callback, pattern=r"^safety:"))

    async def post_init(application) -> None:
        for uid, name in config.ALLOWED_USERS.items():
            greeting = f"Hey {name}! " if name else ""
            try:
                await application.bot.send_message(
                    uid, t("bot_started", greeting=greeting)
                )
            except Exception as exc:
                logger.warning("Could not send startup message to %d: %s", uid, exc)

    # Heartbeat: write timestamp every 30s so the watchdog can detect hangs
    _heartbeat_path = Path(tempfile.gettempdir()) / "tgbot_heartbeat"

    async def _heartbeat(_: object) -> None:
        try:
            _heartbeat_path.write_text(str(time.time()))
        except OSError:
            pass

    app.post_init = post_init
    app.job_queue.run_repeating(_heartbeat, interval=30, first=0)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
