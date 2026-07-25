"""Shared logic for agy and grok CLI backends.

Contains session isolation, preference persistence, output cleanup,
dangerous prompt detection, and process management helpers used by
both agy.py and grok.py.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys

from .config import config

logger = logging.getLogger("wechatbridge.runner")

# ANSI escape code pattern
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# HTML tag pattern
HTML_TAG_RE = re.compile(r"<[^>]+>")

# Sensitive env var prefixes to strip from child process environments
_SENSITIVE_PREFIXES = (
    "TOKEN", "KEY", "SECRET", "PASSWORD",
    "AWS", "GITHUB", "GITLAB", "CREDENTIAL",
)


def sanitize_user_id(user_id: str) -> str:
    """Convert a WeChat user ID to a filesystem-safe directory name.

    Uses a short hash suffix for uniqueness while keeping a readable prefix.
    """
    h = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", user_id)[:48]
    return f"{safe}_{h}"


def get_session_dir(user_id: str) -> str:
    """Get the per-user session directory path."""
    return os.path.join(config.session_base_dir, sanitize_user_id(user_id))


def is_first_message(session_dir: str) -> bool:
    """Check if this user has no existing conversation."""
    return not os.path.exists(os.path.join(session_dir, ".initialized"))


def mark_initialized(session_dir: str) -> None:
    """Create .initialized flag file after first message."""
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, ".initialized"), "w") as f:
            f.write("1")
    except OSError as e:
        logger.error("Failed to mark session initialized: %s", e)


def clean_output(text: str) -> str:
    """Remove ANSI escape codes and HTML tags from CLI output."""
    text = ANSI_RE.sub("", text)
    text = HTML_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Per-user preference persistence
# ---------------------------------------------------------------------------

def load_prefs(user_id: str) -> dict:
    """Load per-user preferences from prefs.json.

    Returns a dict with keys: model, effort, mode, add_dirs, backend.
    Missing keys are filled from defaults.
    """
    session_dir = get_session_dir(user_id)
    prefs_path = os.path.join(session_dir, "prefs.json")
    default_prefs = {"model": "", "effort": "", "mode": "", "add_dirs": [], "backend": "agy"}
    try:
        if os.path.exists(prefs_path):
            with open(prefs_path, "r") as f:
                data = json.load(f)
            for k in default_prefs:
                if k not in data:
                    data[k] = default_prefs[k]
            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load prefs for %s: %s", user_id, e)
    return dict(default_prefs)


def save_prefs(user_id: str, prefs: dict) -> None:
    """Save per-user preferences to prefs.json."""
    session_dir = get_session_dir(user_id)
    os.makedirs(session_dir, exist_ok=True)
    prefs_path = os.path.join(session_dir, "prefs.json")
    try:
        with open(prefs_path, "w") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("Failed to save prefs for %s: %s", user_id, e)


# ---------------------------------------------------------------------------
# Dangerous prompt detection
# ---------------------------------------------------------------------------

# Confirm gate: hardcoded dangerous keyword fallbacks (used when config.confirm_keywords is empty)
# 宁枉勿纵 — 自然语言危险意图词也触发确认，日常误触多一轮确认即可取消
_DANGEROUS_KEYWORDS = [
    "rm -rf /", "curl |sh", "curl | bash", "wget -o- | sh",
    "删掉", "删除", "清空", "卸载", "格式化",
]


def is_dangerous(prompt: str) -> bool:
    """Check if a prompt contains dangerous keywords.

    Uses config.confirm_keywords if non-empty, otherwise falls back to
    the hardcoded _DANGEROUS_KEYWORDS list (matching existing audit behavior).
    """
    keywords = config.confirm_keywords if config.confirm_keywords else _DANGEROUS_KEYWORDS
    lower = prompt.lower()
    for kw in keywords:
        if kw in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Model/effort parsing
# ---------------------------------------------------------------------------

def parse_model_effort(model: str) -> tuple[str, str | None]:
    """Split 'gemini-3.6-flash-high' -> ('gemini-3.6-flash', 'high').

    Returns (base_model, embedded_effort) where embedded_effort is None if
    the model name does not end with -high, -medium, or -low.
    """
    for suffix in ("-high", "-medium", "-low"):
        if model.endswith(suffix):
            base = model[: -len(suffix)]
            effort = suffix[1:]  # strip leading dash
            return base, effort
    return model, None


# ---------------------------------------------------------------------------
# Subprocess environment and process management
# ---------------------------------------------------------------------------

def sanitize_env(session_dir: str) -> dict:
    """Build a clean environment dict for CLI subprocesses.

    Strips sensitive vars, sets HOME (and USERPROFILE on Windows) to session_dir
    for per-user isolation.
    """
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(_SENSITIVE_PREFIXES)
    }
    env["HOME"] = session_dir
    if sys.platform == "win32":
        env["USERPROFILE"] = session_dir
    return env


async def terminate_process(process, graceful: bool = True) -> None:
    """Terminate a subprocess with Unix process-group or Windows direct kill.

    graceful: SIGTERM → 2s wait → SIGKILL (Unix); kill() (Windows).
    Non-graceful: SIGKILL immediately (Unix); kill() (Windows).
    """
    if not process or not process.pid:
        return
    try:
        if hasattr(os, "getpgid") and hasattr(os, "killpg"):
            pgid = os.getpgid(process.pid)
            if graceful:
                os.killpg(pgid, signal.SIGTERM)
                logger.info("Sent SIGTERM to process group %s for graceful lock release", pgid)
                for _ in range(20):
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                if process.returncode is None:
                    os.killpg(pgid, signal.SIGKILL)
                    logger.info("Sent SIGKILL to process group %s after grace period", pgid)
            else:
                os.killpg(pgid, signal.SIGKILL)
        else:
            # Windows: no process groups, kill directly
            process.kill()
            logger.info("Killed process %s directly (non-Unix)", process.pid)
    except (ProcessLookupError, PermissionError, OSError) as e:
        logger.warning("Failed to terminate process: %s", e)
    try:
        await process.wait()
    except Exception:
        pass
