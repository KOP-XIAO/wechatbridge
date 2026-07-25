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
# WeChat error reply format (fixed header + body)
# ---------------------------------------------------------------------------

# Shown when CLI returns successfully but with no display text
EMPTY_REPLY = "（无回复内容）"


def format_error(title: str, detail: str = "") -> str:
    """Standard error bubble: header line only, body below.

    Example:
        ❌ **未登录** ❌

        Grok 凭证不可用。
    """
    title = (title or "错误").strip().replace("\n", " ")
    detail = (detail or "").strip()
    if detail:
        return f"❌ **{title}** ❌\n\n{detail}"
    return f"❌ **{title}** ❌"


def format_cli_error(raw_message: str, *, backend: str = "") -> str:
    """Map CLI stderr/JSON error text into Chinese title + fixed header.

    Never put the whole raw English blob into the ❌ **...** ❌ title line.
    Known cases get a Chinese title + short Chinese explanation; raw text
    is kept under「原始信息」only when it is not already Chinese-heavy.
    """
    raw = clean_output(raw_message or "") or "未知错误"
    lower = raw.lower()
    backend = (backend or "").strip().lower()
    name = "Grok" if backend == "grok" else ("agy" if backend == "agy" else "CLI")

    def _with_raw(title: str, zh: str) -> str:
        # Avoid duplicating if raw is already the zh line
        if raw.strip() == zh.strip():
            return format_error(title, zh)
        return format_error(title, f"{zh}\n\n原始信息：\n{raw}")

    # Auth / login
    if (
        "not signed in" in lower
        or "authenticate" in lower
        or "login --device" in lower
        or "grok login" in lower
        or "please log in" in lower
        or "please login" in lower
        or "not authenticated" in lower
        or "unauthorized" in lower
        or "401" in lower and ("auth" in lower or "token" in lower or "login" in lower)
        or ("xai_api_key" in lower and ("sign" in lower or "login" in lower or "auth" in lower))
    ):
        return _with_raw(
            "未登录",
            f"{name} 未登录，或读不到有效凭证。"
            + (" 请在本机执行 `grok login --device-code`，或设置 `XAI_API_KEY`。" if backend == "grok" else " 请检查本机登录/凭证是否有效。"),
        )

    # Rate limit / quota
    if (
        "rate limit" in lower
        or "rate_limit" in lower
        or "too many requests" in lower
        or "quota" in lower
        or "resource exhausted" in lower
        or "429" in lower
    ):
        return _with_raw("请求过于频繁", "触发限流或额度不足，请稍后再试。")

    # Network
    if (
        "connection refused" in lower
        or "connection reset" in lower
        or "network is unreachable" in lower
        or "name or service not known" in lower
        or "temporary failure in name resolution" in lower
        or "ssl" in lower and ("error" in lower or "certificate" in lower)
        or "econnreset" in lower
        or "econnrefused" in lower
        or "fetch failed" in lower
        or "socket hang up" in lower
    ):
        return _with_raw("网络错误", "连不上服务，请检查网络后重试。")

    # Cascade / API hang (agy)
    if "timeout waiting for cascade" in lower or "timeout waiting for response" in lower:
        return _with_raw("级联超时", "模型 API 级联推理超时，请稍后重试或简化指令。")

    if "permission" in lower and ("denied" in lower or "refuse" in lower or "rejected" in lower):
        return _with_raw("权限不足", "没有执行该操作的权限。")

    if "timeout" in lower or "timed out" in lower or "deadline exceeded" in lower:
        return _with_raw("超时", "等待响应超时，请稍后重试。")

    if "model" in lower and (
        "not found" in lower
        or "unknown" in lower
        or "invalid" in lower
        or "does not exist" in lower
        or "unsupported" in lower
        or "not supported" in lower
        or "no such" in lower
    ):
        return _with_raw("模型无效", "指定的模型不可用，请用 `/models` 查看后重选。")

    if "command not found" in lower or "not a command" in lower:
        return _with_raw("命令不可用", f"{name} 可执行文件可能未安装或不在 PATH 中。")

    if "not found" in lower or "no such file" in lower or "enoent" in lower:
        return _with_raw("未找到", "请求的资源或文件不存在。")

    # Generic CLI failure — short Chinese title, body is raw (may still be English)
    title = "执行失败"
    if backend == "grok":
        title = "Grok 执行失败"
    elif backend == "agy":
        title = "agy 执行失败"
    return format_error(title, raw)


# ---------------------------------------------------------------------------
# Per-user preference persistence (per-backend model/effort/mode memory)
# ---------------------------------------------------------------------------

KNOWN_BACKENDS = ("agy", "grok")
BACKEND_SCOPED_KEYS = ("model", "effort", "mode")


def _default_backend() -> str:
    b = getattr(config, "backend", "agy") or "agy"
    return b if b in KNOWN_BACKENDS else "agy"


def _empty_backend_slot() -> dict:
    return {"model": "", "effort": "", "mode": ""}


def _slot_from(data) -> dict:
    """Normalize a by_backend slot to model/effort/mode strings."""
    if not isinstance(data, dict):
        return _empty_backend_slot()
    return {
        "model": data.get("model") or "",
        "effort": data.get("effort") or "",
        "mode": data.get("mode") or "",
    }


def default_prefs() -> dict:
    """Fresh prefs: empty model/effort/mode means CLI built-in default."""
    backend = _default_backend()
    return {
        "model": "",
        "effort": "",
        "mode": "",
        "add_dirs": [],
        "backend": backend,
        "by_backend": {b: _empty_backend_slot() for b in KNOWN_BACKENDS},
    }


def normalize_prefs(data: dict | None) -> dict:
    """Fill defaults, migrate flat prefs → by_backend, ensure structure.

    Migration (no by_backend yet): copy top-level model/effort/mode into the
    *current* backend slot only; other backends stay empty (project default).
    """
    base = default_prefs()
    if not isinstance(data, dict):
        return base

    backend = data.get("backend") or base["backend"]
    if backend not in KNOWN_BACKENDS:
        backend = base["backend"]

    model = data.get("model") if data.get("model") is not None else ""
    effort = data.get("effort") if data.get("effort") is not None else ""
    mode = data.get("mode") if data.get("mode") is not None else ""
    if not isinstance(model, str):
        model = str(model) if model else ""
    if not isinstance(effort, str):
        effort = str(effort) if effort else ""
    if not isinstance(mode, str):
        mode = str(mode) if mode else ""

    add_dirs = data.get("add_dirs", [])
    if not isinstance(add_dirs, list):
        add_dirs = []

    raw_by = data.get("by_backend")
    if isinstance(raw_by, dict):
        by_backend = {b: _slot_from(raw_by.get(b)) for b in KNOWN_BACKENDS}
        # Keep any extra backend keys only if well-formed (forward-compatible)
        for k, v in raw_by.items():
            if k not in by_backend and isinstance(v, dict):
                by_backend[k] = _slot_from(v)
    else:
        # Legacy flat file: attribute current active fields to current backend only
        by_backend = {b: _empty_backend_slot() for b in KNOWN_BACKENDS}
        by_backend[backend] = {
            "model": model or "",
            "effort": effort or "",
            "mode": mode or "",
        }

    return {
        "model": model or "",
        "effort": effort or "",
        "mode": mode or "",
        "add_dirs": add_dirs,
        "backend": backend,
        "by_backend": by_backend,
    }


def sync_active_to_memory(prefs: dict) -> None:
    """Write top-level model/effort/mode into by_backend[current backend]."""
    backend = prefs.get("backend") or _default_backend()
    if backend not in KNOWN_BACKENDS:
        backend = _default_backend()
        prefs["backend"] = backend
    by = prefs.setdefault("by_backend", {})
    if not isinstance(by, dict):
        by = {}
        prefs["by_backend"] = by
    slot = _empty_backend_slot()
    for k in BACKEND_SCOPED_KEYS:
        slot[k] = prefs.get(k) or ""
    by[backend] = slot
    # Ensure sibling backends exist
    for b in KNOWN_BACKENDS:
        if b not in by or not isinstance(by.get(b), dict):
            by[b] = _empty_backend_slot()


def apply_memory_to_active(prefs: dict, backend: str) -> None:
    """Load by_backend[backend] into top-level model/effort/mode.

    Empty slot → empty active fields (CLI default / project default).
    """
    if backend not in KNOWN_BACKENDS:
        backend = _default_backend()
    by = prefs.get("by_backend") if isinstance(prefs.get("by_backend"), dict) else {}
    slot = _slot_from(by.get(backend))
    for k in BACKEND_SCOPED_KEYS:
        prefs[k] = slot.get(k) or ""


def switch_backend_prefs(prefs: dict, new_backend: str) -> tuple[str, str]:
    """Snapshot current backend memory, switch, restore target memory.

    Returns (old_backend, new_backend). Mutates prefs in place.
    First visit to a backend leaves model/effort/mode empty (CLI default).
    """
    if new_backend not in KNOWN_BACKENDS:
        raise ValueError(f"unknown backend: {new_backend}")
    old = prefs.get("backend") or _default_backend()
    if old not in KNOWN_BACKENDS:
        old = _default_backend()
    # Persist whatever is active under the backend we are leaving
    prefs["backend"] = old
    sync_active_to_memory(prefs)
    prefs["backend"] = new_backend
    apply_memory_to_active(prefs, new_backend)
    # Keep target slot materialised even if empty
    sync_active_to_memory(prefs)
    return old, new_backend


def update_active_prefs(user_id: str, **fields) -> dict:
    """Load prefs, update top-level fields, mirror into current backend memory, save.

    Use for /model, /fast, /planning and any backend-scoped preference change.
    Unknown keys are still written to the top level (e.g. add_dirs) but only
    model/effort/mode are mirrored into by_backend.
    """
    prefs = load_prefs(user_id)
    for k, v in fields.items():
        if v is None:
            prefs[k] = "" if k in BACKEND_SCOPED_KEYS else v
        else:
            prefs[k] = v
    if any(k in BACKEND_SCOPED_KEYS for k in fields):
        sync_active_to_memory(prefs)
    save_prefs(user_id, prefs)
    return prefs


def format_model_label(model: str) -> str:
    """Human-readable model for switch replies."""
    model = (model or "").strip()
    if not model:
        return "后端默认（未指定）"
    return model


def load_prefs(user_id: str) -> dict:
    """Load per-user preferences from prefs.json (normalized + migrated).

    After normalize, active model/effort/mode are re-aligned from
    by_backend[current] so a stale top-level field cannot outlive memory.
    """
    session_dir = get_session_dir(user_id)
    prefs_path = os.path.join(session_dir, "prefs.json")
    try:
        if os.path.exists(prefs_path):
            with open(prefs_path, "r") as f:
                data = json.load(f)
            prefs = normalize_prefs(data)
            apply_memory_to_active(prefs, prefs.get("backend") or _default_backend())
            return prefs
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load prefs for %s: %s", user_id, e)
    return default_prefs()


def save_prefs(user_id: str, prefs: dict) -> None:
    """Save per-user preferences to prefs.json (normalized structure)."""
    session_dir = get_session_dir(user_id)
    os.makedirs(session_dir, exist_ok=True)
    prefs_path = os.path.join(session_dir, "prefs.json")
    try:
        payload = normalize_prefs(prefs)
        # Prefer caller's active fields / backend / add_dirs / by_backend
        for k in BACKEND_SCOPED_KEYS:
            if k in prefs:
                payload[k] = prefs.get(k) or ""
        if prefs.get("backend") in KNOWN_BACKENDS:
            payload["backend"] = prefs["backend"]
        if isinstance(prefs.get("add_dirs"), list):
            payload["add_dirs"] = prefs["add_dirs"]
        if isinstance(prefs.get("by_backend"), dict):
            for b, slot in prefs["by_backend"].items():
                payload["by_backend"][b] = _slot_from(slot)
            for b in KNOWN_BACKENDS:
                payload["by_backend"].setdefault(b, _empty_backend_slot())
        # Current backend slot always mirrors active model/effort/mode
        sync_active_to_memory(payload)
        with open(prefs_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        # Keep caller's dict in sync with what was written
        prefs.update(payload)
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
