"""Shared logic for agy, grok, and codex CLI backends.

Contains session isolation, preference persistence, output cleanup,
dangerous prompt detection, and process management helpers used by
agy.py, grok.py, and codex.py.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import sys
import time

from .config import config

logger = logging.getLogger("wechatbridge.runner")

# ANSI escape code pattern
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# HTML tag pattern — 白名单制，避免把代码输出里的 <String>、<T> 等泛型当标签误删
HTML_TAG_RE = re.compile(
    r"</?(?:b|i|em|strong|code|pre|a|br|div|span|p|u|s|ul|ol|li|table|thead|tbody|tr|td|th|h[1-6]|blockquote|font|center|hr)(?:\s[^<>]*)?/?>",
    re.IGNORECASE,
)

# Sensitive env var prefixes to strip from child process environments
_SENSITIVE_PREFIXES = (
    "TOKEN", "KEY", "SECRET", "PASSWORD",
    "AWS", "GITHUB", "GITLAB", "CREDENTIAL",
)
# Segment names that mark a var as secret when they appear as a path part
_SENSITIVE_SEGMENTS = frozenset({
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "PASS", "PWD",
    "AUTH", "CRED", "CREDS", "PRIVATE",
    "CREDENTIAL", "CREDENTIALS", "APIKEY", "API_KEY",
})
_SENSITIVE_SUFFIXES = (
    "_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_PASSWD",
    "_CREDENTIAL", "_CREDENTIALS", "_APIKEY",
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


def ensure_session_dir(user_id: str) -> str:
    """Create per-user session dir with mode 0700 and return its path."""
    path = get_session_dir(user_id)
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as e:
        logger.warning("Failed to ensure session dir %s: %s", path, e)
    return path


def path_is_under(path: str, root: str) -> bool:
    """True if path is the same as root or a realpath child of root."""
    try:
        real = os.path.realpath(path)
        root_real = os.path.realpath(root)
    except OSError:
        return False
    if real == root_real:
        return True
    prefix = root_real if root_real.endswith(os.sep) else root_real + os.sep
    return real.startswith(prefix)


def validate_add_dir(path: str, user_id: str) -> tuple[bool, str]:
    """Validate /add-dir path: must exist as dir and sit under allowed roots.

    Session dir is always allowed; extra roots from config.add_dir_roots.
    Returns (ok, message_or_resolved_path).
    """
    raw = (path or "").strip()
    if not raw:
        return False, "路径为空"
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        # Relative paths resolve against session dir
        expanded = os.path.join(get_session_dir(user_id), expanded)
    try:
        resolved = os.path.realpath(expanded)
    except OSError as e:
        return False, f"无法解析路径: {e}"
    if not os.path.isdir(resolved):
        return False, "路径不存在或不是目录"

    allowed_roots = [get_session_dir(user_id)]
    for r in getattr(config, "add_dir_roots", []) or []:
        if r:
            allowed_roots.append(os.path.expanduser(r))

    if not any(path_is_under(resolved, root) for root in allowed_roots):
        return False, (
            "路径不在允许范围内。"
            "仅允许你的工作区"
            + (" 或管理员已开放的目录" if config.add_dir_roots else "")
            + "。"
        )
    return True, resolved


def is_first_message(session_dir: str, backend: str = "") -> bool:
    """Check if this user has no existing conversation for the given backend.

    Uses a per-backend flag file ``.initialized.<backend>`` so that agy and
    grok sessions are tracked independently.  When *backend* is empty the
    legacy shared ``.initialized`` file is checked (backward compatibility).
    """
    if backend:
        return not os.path.exists(
            os.path.join(session_dir, f".initialized.{backend}")
        )
    return not os.path.exists(os.path.join(session_dir, ".initialized"))


def mark_initialized(session_dir: str, backend: str = "") -> None:
    """Create .initialized flag file after first message.

    When *backend* is given, writes ``.initialized.<backend>`` instead of
    the legacy shared ``.initialized`` — this prevents cross-backend
    contamination (e.g. an agy success marking grok as "initialized").
    """
    flag_name = f".initialized.{backend}" if backend else ".initialized"
    try:
        os.makedirs(session_dir, exist_ok=True)
        try:
            os.chmod(session_dir, 0o700)
        except OSError:
            pass
        with open(os.path.join(session_dir, flag_name), "w") as f:
            f.write("1")
    except OSError as e:
        logger.error("Failed to mark session initialized: %s", e)


def clear_initialized(session_dir: str, backend: str = "") -> None:
    """Remove .initialized flag(s) to force a fresh session on next message.

    When *backend* is given, removes ``.initialized.<backend>`` and also the
    legacy shared ``.initialized`` (so old installs are cleaned up too).
    When *backend* is empty, removes the legacy shared flag only.
    """
    names = [f".initialized.{backend}", ".initialized"] if backend else [".initialized"]
    for name in names:
        flag = os.path.join(session_dir, name)
        try:
            if os.path.exists(flag):
                os.remove(flag)
        except OSError as e:
            logger.warning("Failed to clear %s: %s", flag, e)


def clean_output(text: str) -> str:
    """Remove ANSI escape codes and HTML tags from CLI output."""
    text = ANSI_RE.sub("", text)
    text = HTML_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# WeChat error reply format (fixed header + body)
# ---------------------------------------------------------------------------

# Shown when backend returns successfully but with no display text
EMPTY_REPLY = "（这次没有文字回复）"

# Titles used for upstream throttle / quota user replies (🔔 notice style).
# Keep in sync with format_cli_error branches and is_upstream_throttle_reply.
# Short-window throttle (retryable by default) vs account/daily quota (no spam retry).
RETRYABLE_THROTTLE_TITLES = (
    "助手通道繁忙",
    "请求较多",
)
QUOTA_TITLES = (
    "额度相关",
)
THROTTLE_TITLES = RETRYABLE_THROTTLE_TITLES + QUOTA_TITLES

# Header of format_error / format_notice: "❌ **title** ❌" or "🔔 **title** 🔔"
_BRIDGE_BUBBLE_HEADER_RE = re.compile(
    r"^(?P<mark>[❌🔔])\s+\*\*[^*]+?\*\*\s+(?P=mark)\s*$"
)


def format_error(title: str, detail: str = "", *, emoji: str = "❌") -> str:
    """Standard error/notice bubble: header line only, body below.

    Example:
        ❌ **未登录** ❌

        助手尚未登录，请联系管理员。

    ``emoji`` defaults to ❌; pass 🔔 for throttle/notice-style messages.
    """
    mark = (emoji or "❌").strip() or "❌"
    title = (title or "错误").strip().replace("\n", " ")
    detail = (detail or "").strip()
    if detail:
        return f"{mark} **{title}** {mark}\n\n{detail}"
    return f"{mark} **{title}** {mark}"


def format_notice(title: str, detail: str = "") -> str:
    """User-facing notice bubble with 🔔 (throttle / cooldown / retry, etc.).

    Example:
        🔔 **上游繁忙，正在重试** 🔔

        第 1/2 次重试，请稍候…
    """
    return format_error(title, detail, emoji="🔔")


def is_bridge_formatted_reply(text: str) -> bool:
    """True if *text* is already a format_error / format_notice bubble.

    Used so backends never re-run format_cli_error on an already-formatted
    display (which would wash 🔔 throttle copy into a generic ❌ failure),
    and so zero-exit structured errors count as failed (no mark_initialized).
    """
    if not text or not isinstance(text, str):
        return False
    header = text.lstrip().split("\n", 1)[0].strip()
    return bool(_BRIDGE_BUBBLE_HEADER_RE.match(header))


def format_oversized_artifact_notice(art_name: str, size_mb: float) -> str:
    """User-facing text when an artifact is too large to send back to WeChat.

    Must never include server absolute paths — only the display name and size.
    """
    name = (art_name or "file").replace("`", "'").strip() or "file"
    return (
        f"⚠️ **文件过大** ⚠️\n\n"
        f"`{name}` {float(size_mb):.1f} MB\n"
        f"文件已生成，但太大无法发到微信。请联系管理员获取，或让我改小后再试。"
    )


def format_cli_error(raw_message: str, *, backend: str = "") -> str:
    """Map backend stderr/JSON error text into a short Chinese user reply.

    Never put English raw blobs or internal path/env names into user text.
    Details stay in the server log only.
    """
    raw = clean_output(raw_message or "") or "未知错误"
    lower = raw.lower()
    backend = (backend or "").strip().lower()
    logger.info(
        "format_cli_error backend=%s raw=%.300s",
        backend or "?",
        raw,
    )

    # codex-specific auth/login recognition (backend-scoped, precise).
    # Only explicit login semantics match, so ordinary API errors, rate
    # limits and model errors are never misclassified as a login problem.
    # agy/grok keep using the generic block below (results unchanged).
    if backend == "codex":
        if (
            "codex login" in lower
            or "codex_api_key" in lower
            or "codex api key" in lower
            or "codex api-key" in lower
            or "authentication required" in lower
            or "not authenticated" in lower
            or "not logged in" in lower
            or "logged out" in lower
            or "login required" in lower
            or "log in to continue" in lower
            or "login to continue" in lower
            or "must be logged in" in lower
            or "you must be logged in" in lower
            or "no valid credentials" in lower
            or "missing credentials" in lower
            or "invalid credentials" in lower
            or "credentials required" in lower
            or "please log in" in lower
            or "please login" in lower
            or "unauthorized" in lower
            or "401" in lower and ("auth" in lower or "token" in lower or "login" in lower)
        ):
            return format_error(
                "未登录",
                "助手尚未登录或凭证失效，请联系管理员处理。",
            )

    # Auth / login — ops details stay in logs; users contact admin
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
        or "api_key" in lower and ("missing" in lower or "invalid" in lower or "required" in lower)
    ):
        return format_error(
            "未登录",
            "助手尚未登录或凭证失效，请联系管理员处理。",
        )

    # Upstream eligibility / control-plane RESOURCE_EXHAUSTED (short-window throttle).
    # Typical agy stderr: "Eligibility check failed: RESOURCE_EXHAUSTED (code 429):
    # Resource has been exhausted (e.g. check quota)." — not daily-quota zero,
    # not bridge concurrency, not user spam. More specific patterns first.
    if (
        "eligibility" in lower
        or "resource_exhausted" in lower
        or "resource exhausted" in lower
    ):
        return format_notice(
            "助手通道繁忙",
            "上游助手通道暂时限流或繁忙，请稍等片刻再试。",
        )

    # Explicit account / daily quota (after resource-exhausted so "check quota"
    # in that message does not steal this branch).
    # Require exceed/limit/daily/… semantics — bare "quota usage report" must not match.
    if (
        "quota exceeded" in lower
        or "quota_exceeded" in lower
        or "exceeded your quota" in lower
        or "daily quota" in lower
        or "usage limit" in lower
        or (
            "quota" in lower
            and (
                "exceed" in lower
                or "exhaust" in lower
                or "limit" in lower
                or "daily" in lower
                or "insufficient" in lower
            )
        )
    ):
        return format_notice(
            "额度相关",
            "当前额度或配额可能受限，请稍后再试或联系管理员。",
        )

    # Explicit rate limit / too many requests (no resource-exhausted wording).
    if (
        "rate limit" in lower
        or "rate_limit" in lower
        or "too many requests" in lower
    ):
        return format_notice(
            "请求较多",
            "当前请求较多，请稍后再试。",
        )

    # Bare 429 fallback — word-boundary so "1429" / "x4290" do not match.
    # Also accept explicit "status 429" / "code 429" forms.
    if re.search(r"\b429\b", lower) or re.search(
        r"(?:status|code|http)\s*[:=]?\s*429\b", lower
    ):
        return format_notice(
            "助手通道繁忙",
            "上游暂时限流或繁忙，请稍等片刻再试。",
        )

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
        return format_error("网络错误", "连不上服务，请检查网络后重试。")

    # Cascade / API hang (agy) — plain language for users
    if "timeout waiting for cascade" in lower or "timeout waiting for response" in lower:
        return format_error(
            "模型响应超时",
            "模型响应超时，请稍后重试或简化指令。",
        )

    if "permission" in lower and ("denied" in lower or "refuse" in lower or "rejected" in lower):
        return format_error("权限不足", "没有执行该操作的权限。")

    if "timeout" in lower or "timed out" in lower or "deadline exceeded" in lower:
        return format_error("超时", "等待响应超时，请稍后重试。")

    if "no session found" in lower:
        return format_error(
            "会话不存在",
            "上一轮对话记录已过期，请重新发一次消息即可。",
        )

    if "model" in lower and (
        "not found" in lower
        or "unknown" in lower
        or "invalid" in lower
        or "does not exist" in lower
        or "unsupported" in lower
        or "not supported" in lower
        or "no such" in lower
    ):
        return format_error("模型无效", "指定的模型不可用，请用 `/models` 查看后重选。")

    if "command not found" in lower or "not a command" in lower:
        return format_error(
            "助手不可用",
            "助手程序未正确安装或配置，请联系管理员。",
        )

    if "not found" in lower or "no such file" in lower or "enoent" in lower:
        return format_error("未找到", "请求的资源或文件不存在。")

    # Unknown: fixed Chinese only — never echo English raw to WeChat users
    return format_error("执行失败", "这次没处理好，请稍后再试。")


def is_upstream_throttle_reply(text: str) -> bool:
    """True if *text* is a bridge-formatted 🔔 throttle/quota user reply.

    Requires a real format_notice bubble (🔔 + **title**) so free-form model
    replies that merely mention e.g. ``**请求较多**`` do not trigger retry.
    Covers both short-window throttle and 额度相关 (caller decides retry policy).
    """
    if not is_bridge_formatted_reply(text):
        return False
    body = text.lstrip()
    if not body.startswith("🔔"):
        return False
    for title in THROTTLE_TITLES:
        if f"**{title}**" in body:
            return True
    return False


def is_upstream_quota_reply(text: str) -> bool:
    """True if *text* is a bridge-formatted 🔔 额度相关 notice (not short-window)."""
    if not is_upstream_throttle_reply(text):
        return False
    return "**额度相关**" in text.lstrip()


def classify_upstream_failure(text: str) -> str | None:
    """Return ``\"throttle\"``, ``\"quota\"``, or None for non-upstream failures."""
    if not is_upstream_throttle_reply(text):
        return None
    if is_upstream_quota_reply(text):
        return "quota"
    return "throttle"


def format_upstream_retry_notice(retry_n: int, retry_max: int) -> str:
    """A: mid-flight retry notice (sent before backoff sleep)."""
    return format_notice(
        "上游繁忙，正在重试",
        f"第 {retry_n}/{retry_max} 次重试，请稍候…",
    )


def format_upstream_cooldown_notice(seconds: int) -> str:
    """B: global cooldown notice (sent before waiting out the cooldown)."""
    secs = max(1, int(seconds))
    return format_notice(
        "上游冷却中",
        f"约 {secs} 秒后自动继续。",
    )


class UpstreamGuard:
    """Process-wide upstream throttle / jitter state (agy + grok + codex).

    - global_cooldown_until: after any throttle, cool the whole process
    - user_gap_until: after a user's throttle, silent min interval for that user
    """

    def __init__(self) -> None:
        self.global_cooldown_until: float = 0.0
        self.user_gap_until: dict = {}

    def mark_throttle(self, user_id: str) -> None:
        """Record a throttle signal: extend global cooldown + per-user gap."""
        now = time.time()
        cooldown = max(0, int(getattr(config, "upstream_cooldown", 20) or 0))
        user_gap = max(0, int(getattr(config, "upstream_user_gap", 10) or 0))
        if cooldown:
            self.global_cooldown_until = max(
                self.global_cooldown_until, now + cooldown
            )
        if user_id and user_gap:
            prev = self.user_gap_until.get(user_id, 0.0)
            self.user_gap_until[user_id] = max(prev, now + user_gap)

    def clear_user_gap(self, user_id: str) -> None:
        """Drop per-user gap after a successful (non-throttle) reply."""
        if user_id:
            self.user_gap_until.pop(user_id, None)

    def global_remaining(self) -> float:
        """Seconds left on the global cooldown (0 if idle)."""
        return max(0.0, self.global_cooldown_until - time.time())

    def user_gap_remaining(self, user_id: str) -> float:
        """Seconds left on this user's silent gap (0 if idle)."""
        if not user_id:
            return 0.0
        return max(0.0, self.user_gap_until.get(user_id, 0.0) - time.time())


# Shared process-wide guard instance used by main._run_llm_with_guard
upstream_guard = UpstreamGuard()


# ---------------------------------------------------------------------------
# Per-user preference persistence (per-backend model/effort/mode memory)
# ---------------------------------------------------------------------------

KNOWN_BACKENDS = ("agy", "grok", "codex")
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
        return "默认（未指定）"
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
        # tmp + os.replace 原子写，避免崩溃留下半截 prefs.json
        tmp_path = prefs_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, prefs_path)
        # Keep caller's dict in sync with what was written
        prefs.update(payload)
    except OSError as e:
        logger.error("Failed to save prefs for %s: %s", user_id, e)


# ---------------------------------------------------------------------------
# Dangerous prompt detection
# ---------------------------------------------------------------------------

# Confirm gate: hardcoded dangerous keyword fallbacks (used when config.confirm_keywords is empty)
# Prefer concrete shell/destructive patterns; avoid bare Chinese words like「删除」that false-positive daily chat.
_DANGEROUS_KEYWORDS = [
    "rm -rf /", "rm -rf/*", "rm -rf ~", "rm -rf~",
    "curl |sh", "curl|sh", "curl | bash", "curl|bash",
    "wget -o- | sh", "wget|sh", "wget|bash",
    "mkfs.", "mkfs ", "dd if=", ":(){", "fork bomb",
    "chmod -r 777 /", "chmod -r 777/",
    "drop table", "drop database",
    "format c:", "del /f /s",
    "格式化磁盘", "格式化硬盘", "清空系统", "删掉所有", "删除全部",
    "卸载系统",
]


def is_dangerous(prompt: str) -> bool:
    """Check if a prompt contains dangerous keywords.

    Uses config.confirm_keywords if non-empty, otherwise falls back to
    the hardcoded _DANGEROUS_KEYWORDS list.
    """
    keywords = config.confirm_keywords if config.confirm_keywords else _DANGEROUS_KEYWORDS
    lower = prompt.lower()
    for kw in keywords:
        if kw.lower() in lower:
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

def _is_sensitive_env_name(name: str) -> bool:
    """True if env var name looks like a secret (prefix, segment, or suffix)."""
    u = (name or "").upper()
    if not u:
        return False
    if u.startswith(_SENSITIVE_PREFIXES):
        return True
    if u.endswith(_SENSITIVE_SUFFIXES):
        return True
    if "API_KEY" in u or "ACCESS_KEY" in u or "SECRET_KEY" in u or "AUTH_TOKEN" in u:
        return True
    for part in re.split(r"[._-]+", u):
        if part in _SENSITIVE_SEGMENTS:
            return True
    return False


def sanitize_env(session_dir: str) -> dict:
    """Build a clean environment dict for CLI subprocesses.

    Strips sensitive vars (including XAI_API_KEY / OPENAI_API_KEY style names),
    sets HOME (and USERPROFILE on Windows) to session_dir for per-user isolation.
    """
    env = {
        k: v for k, v in os.environ.items()
        if not _is_sensitive_env_name(k)
    }
    env["HOME"] = session_dir
    if sys.platform == "win32":
        env["USERPROFILE"] = session_dir
    return env


# Temporary / cache roots under each user session (safe to expire file-by-file).
_SESSION_TEMP_REL_DIRS = (
    "images",
    "files",
    ".cache",
    os.path.join(".gemini", "antigravity-cli", "scratch"),
    os.path.join(".gemini", "antigravity-cli", "crashes"),
    os.path.join(".gemini", "antigravity-cli", "log"),
    os.path.join(".gemini", "antigravity-cli", "cache"),
    os.path.join(".grok", "logs"),
    os.path.join(".codex", "logs"),
)

# Dialogue history — cleaned as *units* (never split SQLite sidecars / session trees).
_HISTORY_CONVERSATIONS_REL = os.path.join(
    ".gemini", "antigravity-cli", "conversations"
)
_HISTORY_BRAIN_REL = os.path.join(".gemini", "antigravity-cli", "brain")
_HISTORY_KNOWLEDGE_REL = os.path.join(".gemini", "antigravity-cli", "knowledge")
_HISTORY_GROK_SESSIONS_REL = os.path.join(".grok", "sessions")
_HISTORY_CODEX_SESSIONS_REL = os.path.join(".codex", "sessions")

_SESSION_HISTORY_REL_DIRS = (
    _HISTORY_CONVERSATIONS_REL,
    _HISTORY_BRAIN_REL,
    _HISTORY_KNOWLEDGE_REL,
    _HISTORY_GROK_SESSIONS_REL,
    _HISTORY_CODEX_SESSIONS_REL,
)

# SQLite sidecar suffixes that must share fate with the main ``*.db`` file.
_SQLITE_SIDECAR_TAILS = ("-wal", "-shm", "-journal")


def _is_dangling_symlink(path: str) -> bool:
    """True if path is a symlink whose target does not resolve.

    Uses ``lstat`` first so we never mistake a plain missing path for a link.
    ``os.path.exists`` follows the final hop — False means dangling. Unlink
    still tolerates races (ENOENT) in the caller.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISLNK(st.st_mode):
        return False
    try:
        return not os.path.exists(path)
    except OSError:
        return True


def _unlink_quiet(path: str) -> bool:
    """Unlink path; True if removed. ENOENT (race) counts as already gone."""
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        logger.warning("Session cleanup failed %s: %s", path, e)
        return False


def _remove_old_files_under(root: str, cutoff: float) -> int:
    """Delete files under root older than cutoff; prune empty subdirs.

    For independent temp files only (images/cache/scratch). Does not remove
    ``root`` itself. Returns number of files removed.

    Dangling symlinks (file or dir links, e.g. stale venv ``bin/python`` /
    ``lib64``) are removed immediately — they are never useful and previously
    caused getmtime noise. Intact files/links still age out by ``lstat`` mtime.
    """
    if not os.path.isdir(root):
        return 0
    removed = 0
    try:
        # followlinks=False: never walk through session symlinks into host trees
        for dirpath, dirnames, filenames in os.walk(
            root, topdown=False, followlinks=False
        ):
            # Dir symlinks (and some platforms' file links) may appear in dirnames
            for dn in list(dirnames):
                dpath = os.path.join(dirpath, dn)
                if _is_dangling_symlink(dpath):
                    if _unlink_quiet(dpath):
                        removed += 1
                        logger.info(
                            "Session cleanup: removed dangling dir link %s", dpath
                        )
            for fn in filenames:
                path = os.path.join(dirpath, fn)
                try:
                    if os.path.islink(path):
                        # 悬空链接立刻删；完好链接仍按自身 mtime 过期
                        if _is_dangling_symlink(path) or os.lstat(path).st_mtime < cutoff:
                            if _unlink_quiet(path):
                                removed += 1
                                logger.info("Session cleanup: removed %s", path)
                        continue
                    if not os.path.isfile(path):
                        continue
                    if os.lstat(path).st_mtime < cutoff:
                        os.remove(path)
                        removed += 1
                        logger.info("Session cleanup: removed %s", path)
                except FileNotFoundError:
                    # raced with another cleaner / process
                    continue
                except OSError as e:
                    logger.warning("Session cleanup failed %s: %s", path, e)
            if dirpath == root:
                continue
            try:
                # With followlinks=False, dirpath is a real dir we entered — only
                # prune when empty (never follow/remove host trees via symlinks).
                if not os.path.islink(dirpath) and not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
    except OSError as e:
        logger.warning("Session cleanup walk failed %s: %s", root, e)
    return removed


def _file_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _tree_newest_mtime(root: str) -> float | None:
    """Newest *file* mtime under root (directory mtimes are ignored).

    Parent dirs get a fresh mtime on mkdir/unlink of children; using them would
    keep idle trees forever or falsely mark them active. Empty trees fall back
    to the directory mtime so empty shells can still be pruned.
    """
    if os.path.isfile(root) or os.path.islink(root):
        return _file_mtime(root)
    if not os.path.isdir(root):
        return _file_mtime(root)
    newest: float | None = None
    try:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                m = _file_mtime(os.path.join(dirpath, fn))
                if m is not None and (newest is None or m > newest):
                    newest = m
    except OSError as e:
        logger.warning("Session cleanup mtime walk failed %s: %s", root, e)
    if newest is None:
        return _file_mtime(root)
    return newest


def _paths_newest_mtime(paths: list[str]) -> float | None:
    newest: float | None = None
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            m = _tree_newest_mtime(path)
        else:
            m = _file_mtime(path)
        if m is not None and (newest is None or m > newest):
            newest = m
    return newest


def _delete_path_unit(path: str) -> int:
    """Remove a file or directory tree. Returns number of *files* removed (est.)."""
    removed = 0
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            for dirpath, _, filenames in os.walk(path):
                removed += len(filenames)
            shutil.rmtree(path)
            logger.info("Session cleanup: removed tree %s", path)
        elif os.path.lexists(path):
            os.remove(path)
            removed = 1
            logger.info("Session cleanup: removed %s", path)
    except OSError as e:
        logger.warning("Session cleanup failed %s: %s", path, e)
    return removed


def _remove_idle_unit(paths: list[str], cutoff: float) -> int:
    """If the unit's newest mtime is older than cutoff, delete every path in it.

    Re-checks mtime immediately before delete (narrows TOCTOU). Keeps the whole
    unit if *any* member is still fresh — never splits SQLite sidecars.
    """
    paths = [p for p in paths if os.path.lexists(p)]
    if not paths:
        return 0
    newest = _paths_newest_mtime(paths)
    if newest is None or newest >= cutoff:
        return 0
    # Re-check right before mutating
    newest2 = _paths_newest_mtime(paths)
    if newest2 is None or newest2 >= cutoff:
        return 0
    removed = 0
    for path in paths:
        removed += _delete_path_unit(path)
    return removed


def _sqlite_unit_key(filename: str) -> str | None:
    """Map ``id.db`` / ``id.db-wal`` / ``id.db-shm`` / ``id.db-journal`` → ``id.db``.

    Returns None if the name is not a SQLite main DB or known sidecar.
    """
    if filename.endswith(".db"):
        return filename
    for tail in _SQLITE_SIDECAR_TAILS:
        # e.g. foo.db-wal → foo.db
        suf = ".db" + tail
        if filename.endswith(suf):
            return filename[: -len(tail)]
    return None


def _clean_conversation_dbs(conv_dir: str, cutoff: float) -> int:
    """Expire idle agy conversation SQLite DBs as whole units (db+wal+shm)."""
    if not os.path.isdir(conv_dir):
        return 0
    groups: dict[str, list[str]] = {}
    loose: list[str] = []
    try:
        names = os.listdir(conv_dir)
    except OSError as e:
        logger.warning("Session cleanup list failed %s: %s", conv_dir, e)
        return 0
    for name in names:
        path = os.path.join(conv_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            # Unexpected subdir: treat as tree unit
            loose.append(path)
            continue
        key = _sqlite_unit_key(name)
        if key is not None:
            groups.setdefault(key, []).append(path)
        else:
            loose.append(path)
    removed = 0
    for key, members in groups.items():
        removed += _remove_idle_unit(members, cutoff)
    for path in loose:
        removed += _remove_idle_unit([path], cutoff)
    return removed


def _clean_child_units(parent: str, cutoff: float) -> int:
    """Expire each direct child (file or directory tree) by its newest mtime."""
    if not os.path.isdir(parent):
        return 0
    removed = 0
    try:
        names = os.listdir(parent)
    except OSError as e:
        logger.warning("Session cleanup list failed %s: %s", parent, e)
        return 0
    for name in names:
        path = os.path.join(parent, name)
        removed += _remove_idle_unit([path], cutoff)
    return removed


def _clean_grok_sessions(sessions_root: str, cutoff: float) -> int:
    """Expire idle grok session trees: sessions/<cwd-key>/<session-id>/ as units.

    Top-level index files (e.g. session_search.sqlite) are their own units.
    Empty cwd-key dirs are pruned afterward.
    """
    if not os.path.isdir(sessions_root):
        return 0
    removed = 0
    try:
        names = os.listdir(sessions_root)
    except OSError as e:
        logger.warning("Session cleanup list failed %s: %s", sessions_root, e)
        return 0
    for name in names:
        path = os.path.join(sessions_root, name)
        if os.path.isfile(path) or os.path.islink(path):
            removed += _remove_idle_unit([path], cutoff)
            continue
        if not os.path.isdir(path):
            continue
        # cwd-key bucket: expire each session-id child as a full tree
        try:
            children = os.listdir(path)
        except OSError as e:
            logger.warning("Session cleanup list failed %s: %s", path, e)
            continue
        for child in children:
            child_path = os.path.join(path, child)
            removed += _remove_idle_unit([child_path], cutoff)
        try:
            if not os.listdir(path):
                os.rmdir(path)
                logger.info("Session cleanup: removed empty dir %s", path)
        except OSError:
            pass
    return removed


def _clean_codex_sessions(sessions_root: str, cutoff: float) -> int:
    """Expire idle codex rollout files: sessions/YYYY/MM/DD/rollout-*.jsonl(.zst).

    Unlike grok (which buckets by cwd-key), codex stores per-session rollouts
    under date buckets. We expire individual rollout files by mtime (each file
    is one session) and prune now-empty day/month/year directories.

    Compressed rollouts (``rollout-*.jsonl.zst`` — feature off by default but
    forward-compatible with /tmp/codex-src) are treated exactly like ``.jsonl``.
    Only files whose name is ``rollout-*.jsonl`` or ``rollout-*.jsonl.zst`` are
    ever touched here; auth.json / config / AGENTS.md / other session files are
    never deleted by this function.
    """
    if not os.path.isdir(sessions_root):
        return 0
    removed = 0
    try:
        years = os.listdir(sessions_root)
    except OSError as e:
        logger.warning("Session cleanup list failed %s: %s", sessions_root, e)
        return 0
    for year in years:
        yp = os.path.join(sessions_root, year)
        if not os.path.isdir(yp):
            continue
        try:
            months = os.listdir(yp)
        except OSError as e:
            logger.warning("Session cleanup list failed %s: %s", yp, e)
            continue
        for month in months:
            mp = os.path.join(yp, month)
            if not os.path.isdir(mp):
                continue
            try:
                days = os.listdir(mp)
            except OSError as e:
                logger.warning("Session cleanup list failed %s: %s", mp, e)
                continue
            for day in days:
                dp = os.path.join(mp, day)
                if not os.path.isdir(dp):
                    continue
                try:
                    files = os.listdir(dp)
                except OSError as e:
                    logger.warning("Session cleanup list failed %s: %s", dp, e)
                    continue
                for fn in files:
                    if not (
                        fn.startswith("rollout-")
                        and (fn.endswith(".jsonl") or fn.endswith(".jsonl.zst"))
                    ):
                        continue
                    fp = os.path.join(dp, fn)
                    try:
                        if os.path.isfile(fp) and os.lstat(fp).st_mtime < cutoff:
                            os.remove(fp)
                            removed += 1
                            logger.info("Session cleanup: removed codex rollout %s", fp)
                    except OSError as e:
                        logger.warning("Session cleanup failed %s: %s", fp, e)
                _try_rmdir_empty(dp)
            _try_rmdir_empty(mp)
        _try_rmdir_empty(yp)
    return removed


def _try_rmdir_empty(path: str) -> None:
    """Remove dir if it is now empty (used by codex date-bucket pruning)."""
    try:
        if not os.listdir(path):
            os.rmdir(path)
            logger.info("Session cleanup: removed empty dir %s", path)
    except OSError:
        pass


def _clean_user_history(user_dir: str, cutoff: float) -> int:
    """Unit-based dialogue history cleanup for one user session directory."""
    removed = 0
    removed += _clean_conversation_dbs(
        os.path.join(user_dir, _HISTORY_CONVERSATIONS_REL), cutoff
    )
    removed += _clean_child_units(
        os.path.join(user_dir, _HISTORY_BRAIN_REL), cutoff
    )
    removed += _clean_child_units(
        os.path.join(user_dir, _HISTORY_KNOWLEDGE_REL), cutoff
    )
    removed += _clean_grok_sessions(
        os.path.join(user_dir, _HISTORY_GROK_SESSIONS_REL), cutoff
    )
    removed += _clean_codex_sessions(
        os.path.join(user_dir, _HISTORY_CODEX_SESSIONS_REL), cutoff
    )
    return removed


def _dir_has_any_file(root: str) -> bool:
    if not os.path.isdir(root):
        return False
    try:
        for dirpath, _, filenames in os.walk(root):
            if filenames:
                return True
    except OSError:
        return False
    return False


def _clear_initialized_if_no_history(user_dir: str) -> dict:
    """Clear per-backend .initialized flags when that backend's history is gone.

    Returns a dict mapping cleared backend names to True.
    """
    cleared: dict[str, bool] = {}

    # grok: check only grok session dirs
    grok_has = _dir_has_any_file(os.path.join(user_dir, _HISTORY_GROK_SESSIONS_REL))
    if not grok_has:
        flag = os.path.join(user_dir, ".initialized.grok")
        if os.path.exists(flag):
            try:
                os.remove(flag)
                cleared["grok"] = True
                logger.info(
                    "Session cleanup: cleared .initialized.grok for %s (no grok history left)",
                    user_dir,
                )
            except OSError as e:
                logger.warning("Session cleanup: failed to clear %s: %s", flag, e)

    # codex: check only codex session dirs
    codex_has = _dir_has_any_file(os.path.join(user_dir, _HISTORY_CODEX_SESSIONS_REL))
    if not codex_has:
        flag = os.path.join(user_dir, ".initialized.codex")
        if os.path.exists(flag):
            try:
                os.remove(flag)
                cleared["codex"] = True
                logger.info(
                    "Session cleanup: cleared .initialized.codex for %s (no codex history left)",
                    user_dir,
                )
            except OSError as e:
                logger.warning("Session cleanup: failed to clear %s: %s", flag, e)
        # 会话历史已空，残留的 thread_id 指向已删除的 rollout，一并清掉
        tid = os.path.join(user_dir, ".codex_thread_id")
        if os.path.exists(tid):
            try:
                os.remove(tid)
            except OSError as e:
                logger.warning("Session cleanup: failed to clear %s: %s", tid, e)

    # agy: check only agy conversation/brain/knowledge dirs
    agy_has = any(
        _dir_has_any_file(os.path.join(user_dir, rel))
        for rel in (_HISTORY_CONVERSATIONS_REL, _HISTORY_BRAIN_REL, _HISTORY_KNOWLEDGE_REL)
    )
    if not agy_has:
        flag = os.path.join(user_dir, ".initialized.agy")
        if os.path.exists(flag):
            try:
                os.remove(flag)
                cleared["agy"] = True
                logger.info(
                    "Session cleanup: cleared .initialized.agy for %s (no agy history left)",
                    user_dir,
                )
            except OSError as e:
                logger.warning("Session cleanup: failed to clear %s: %s", flag, e)

    # Legacy: clean shared .initialized if no history at all
    if not grok_has and not agy_has and not codex_has:
        flag = os.path.join(user_dir, ".initialized")
        if os.path.exists(flag):
            try:
                os.remove(flag)
                logger.info(
                    "Session cleanup: cleared legacy .initialized for %s (no history left)",
                    user_dir,
                )
            except OSError as e:
                logger.warning("Session cleanup: failed to clear %s: %s", flag, e)

    return cleared


def clean_session_data(
    retention_days: int | None = None,
    history_retention_days: int | None = None,
) -> int:
    """Remove old session artifacts under each user directory.

    Two TTLs:
      - Temps (default ``session_retention_days``, often 7d): file-level mtime
        under images/files/.cache/scratch/logs/...
      - Dialogue history (default ``history_retention_days``, 30d): **unit** idle
        time = newest mtime in the unit. Units are:
          * agy ``conversations``: each ``*.db`` + ``*.db-wal/shm/journal`` together
          * agy ``brain`` / ``knowledge``: each top-level child tree/file
          * grok ``sessions``: each ``<cwd>/<session-id>/`` tree; top-level indexes alone

    Never splits a unit (prevents half-deleted SQLite DBs). Does **not** touch
    prefs, auth links, or CLI install trees.

    If a user's history dirs are fully empty after cleanup, removes
    ``.initialized`` so the next message starts a new conversation.

    Returns number of removed files (trees count as their file totals).
    """
    if retention_days is None:
        retention_days = config.session_retention_days
    if history_retention_days is None:
        history_retention_days = config.history_retention_days
    base = config.session_base_dir
    if not os.path.isdir(base):
        return 0
    now = time.time()
    temp_cutoff = now - max(int(retention_days), 0) * 86400
    hist_cutoff = now - max(int(history_retention_days), 0) * 86400
    removed = 0
    try:
        for name in os.listdir(base):
            user_dir = os.path.join(base, name)
            if not os.path.isdir(user_dir):
                continue
            for rel in _SESSION_TEMP_REL_DIRS:
                removed += _remove_old_files_under(
                    os.path.join(user_dir, rel), temp_cutoff
                )
            removed += _clean_user_history(user_dir, hist_cutoff)
            _clear_initialized_if_no_history(user_dir)
    except OSError as e:
        logger.error("Session data cleanup error: %s", e)
    return removed


def clean_session_media(retention_days: int | None = None) -> int:
    """Backward-compatible alias for :func:`clean_session_data` (temps + history)."""
    return clean_session_data(retention_days=retention_days)


def split_message_chunks(text: str, limit: int | None = None) -> list[str]:
    """Split a long reply into chunks under limit characters (prefer newlines).

    Cuts by position only — no rstrip/lstrip at boundaries — so
    ''.join(chunks) always equals the original text.
    """
    if limit is None:
        limit = config.message_chunk_chars
    if limit <= 0:
        return [text or ""]
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        window = rest[:limit]
        # Prefer break at newline, then space; never cut at <=0 (would infinite-loop)
        cut = window.rfind("\n")
        if cut <= 0 or cut < limit // 3:
            cut = window.rfind(" ")
        if cut <= 0 or cut < limit // 3:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:]
    # Drop pure-empty pieces only (should not happen with cut > 0)
    return [c for c in chunks if c != ""] or [""]


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
