"""
dsh (DeepSeek Harness CLI) runner with per-user workspace isolation.

Boots the ``headless`` profile for one-shot tasks::

    dsh --profile headless -- "<task>"

The headless bundle always creates a *fresh* session per invocation
(``session-<uuid>``), prints the final assistant message to stdout, writes
``dsh: <code>: <message>`` to stderr on error, and exits 0 only when the turn
completed.  This backend is therefore **single-turn**: every WeChat message
starts a new dsh session, so ``/clear`` / ``/new`` are accepted but are no-ops.

Workspace isolation: the child runs with ``cwd`` = the per-user session
directory (workspace for file artifacts) and ``HOME`` pointed there.
``DSH_HOME`` is pointed to a machine-wide host directory (default ``~/.dsh``,
or ``WECHATBRIDGE_DSH_HOME``) so credentials and profiles are shared host-wide.
As a trade-off, dsh session transcripts are written to the machine-wide
``$DSH_HOME/sessions/`` rather than per-user directories, and are expired by
the background cleanup runner based on age (cutoff).
"""

import asyncio
import logging
import os
import re
import time
from urllib.parse import unquote

from .config import config, host_dsh_home, is_dsh_home_explicit
from .runner_common import (
    clean_output,
    ensure_session_dir,
    format_error,
    format_cli_error,
    is_bridge_formatted_reply,
    is_dangerous,
    is_first_message,
    mark_initialized,
    path_is_under,
    sanitize_env,
    terminate_process,
    EMPTY_REPLY,
)

logger = logging.getLogger("dsh_runner")

_warned_dsh_home_implicit = False

# execve 单参数上限（Linux MAX_ARG_STRLEN = 128KB），留安全余量
_MAX_ARG_BYTES = 120 * 1024

# 从子进程环境里剥掉的 dsh 会话相关变量：桥进程自身可能跑在某个 dsh 会话里
# （本机登录态），不能把这些泄漏给子进程。
_DSH_SESSION_ENV_KEYS = ("DSH_SESSION_ID", "DSH_SESSION_JSONL", "DSH_SHELL")


def _host_dsh_home() -> str:
    """Machine-wide DeepSeek Harness home used by the dsh child process.

    Precedence: ``WECHATBRIDGE_DSH_HOME`` > ``WECHATBRIDGE_HOST_HOME``/``~``.
    The child env sets ``HOME`` to the per-user session dir, so without an
    explicit ``DSH_HOME`` dsh would resolve its home under the session dir and
    find no profiles — we always pass the host home explicitly.
    """
    return host_dsh_home()


_DSH_SKIP_DIR_NAMES = frozenset({
    ".dsh",
    ".gemini",
    ".codex",
    ".grok",
    ".git",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "cache",
    ".cache",
})


_BARE_FILE_URI_RE = re.compile(r"file:///[^\s)\]}>]+")
_TRAILING_PUNCT_CHARS = "。，！？；、：”’'\"）)]}>》」』〉…,.!?;:"


def _parse_bare_file_uri(raw: str) -> tuple[str, str]:
    """Parse bare file:/// URI and separate trailing CJK commentary / punctuation.

    Returns:
        tuple[str, str]: (clean_file_uri, trailing_kept_text)
    """
    cleaned = raw.rstrip(_TRAILING_PUNCT_CHARS)
    punct = raw[len(cleaned):]
    last_slash = cleaned.rfind("/")
    cjk_kept = ""
    if last_slash != -1:
        basename = cleaned[last_slash + 1:]
        if re.search(r"[a-zA-Z0-9._~%+=:-][\u4e00-\u9fff]+$", basename):
            cjk_m = re.search(r"[\u4e00-\u9fff]+$", basename)
            if cjk_m:
                cjk_kept = cjk_m.group(0)
                cleaned = cleaned[:-len(cjk_kept)]
    return cleaned, cjk_kept + punct


def extract_artifacts(text: str, cwd: str = "") -> list[tuple[str, str]]:
    """Extract (name, absolute_path) tuples of file references from dsh output.

    Recognizes (deduplicated, order-preserved):
      - markdown links ``[name](file:///abs/path)`` (agy-compatible)
      - markdown links ``[name](/abs/path)`` and ``[name](./rel/path)`` /
        ``[name](../rel/path)`` — relative paths resolve against *cwd*
      - bare ``file:///abs/path`` mentions

    Non-file URLs (``https://``, ``mailto:``, tool names) are ignored.
    """
    if not text:
        return []
    seen: set = set()
    result: list[tuple[str, str]] = []

    def _add(name: str, path: str) -> None:
        if path.startswith("file://"):
            # 只剥 "file://"（7 字符），保留 file:///abs 的第三个 "/"，
            # 否则 /tmp/a.txt 会变成 tmp/a.txt 被当成相对路径丢弃。
            path = path[len("file://"):]
        if not path.startswith("/"):
            if cwd and (path.startswith("./") or path.startswith("../")):
                path = os.path.normpath(os.path.join(cwd, path))
            else:
                return  # https://, mailto:, bare names, etc.
        name = unquote(name.split("#")[0])
        path = unquote(path.split("#")[0])
        norm_path = os.path.normpath(path)
        parts = norm_path.split(os.sep)
        if any(part in _DSH_SKIP_DIR_NAMES for part in parts):
            return
        # 按路径去重（同一文件只回传一次；首个显示名优先）
        if path not in seen:
            seen.add(path)
            result.append((name, path))

    # [name](file:///path | /abs | ./rel | ../rel)
    for m in re.finditer(
        r"\[([^\]]+)\]\((file:///[^)\s]+|/(?:[^)\s]*)|\.\.?/[^)\s]+)\)", text
    ):
        _add(m.group(1), m.group(2))
    # bare file:///path with greedy match and trailing CJK/punctuation separation
    for m in _BARE_FILE_URI_RE.finditer(text):
        clean_uri, _ = _parse_bare_file_uri(m.group(0))
        path_part = clean_uri[len("file://"):]
        if path_part.startswith("/"):
            _add(os.path.basename(path_part), clean_uri)

    if result:
        logger.debug("Extracted %d artifacts: %s", len(result), [n for n, _ in result[:3]])
    return result


def _strip_file_links(display: str) -> str:
    """Remove file:/// and absolute/relative link targets from display text
    so server paths never leak to WeChat users."""
    out = re.sub(
        r"\[([^\]]+)\]\((?:file:///|/|\.\.?/)[^)]+\)",
        r"[\1]",
        display,
    )
    def _replace_bare(m: re.Match) -> str:
        _, kept = _parse_bare_file_uri(m.group(0))
        return kept

    return _BARE_FILE_URI_RE.sub(_replace_bare, out)


_AT_TOKEN_RE = re.compile(r"@([^\s@。，！？；、：”’'\"）)\]\}>》」』〉…]+)")
_AT_TRAILING_PUNCT = ".,!?;:)>]}'\""


def _sanitize_prompt_at_paths(prompt: str, session_dir: str) -> str:
    """Replace @<path> mentions pointing outside session_dir with [blocked-path].

    Recognizes @ tokens (ASCII, CJK, relative ./ and ../, ~/ and file://).
    Path candidates outside realpath(session_dir) are replaced with [blocked-path].
    Legitimate attachments under session_dir and non-path @mentions are preserved intact.

    Known limitation:
      Paths containing spaces are truncated at the space by token splitting
      and will be judged as blocked paths.
    """
    if not prompt:
        return ""

    def _replace(m: re.Match) -> str:
        raw_token = m.group(1)
        clean_token = raw_token.rstrip(_AT_TRAILING_PUNCT)
        trailing_punct = raw_token[len(clean_token):]

        candidate = clean_token
        # Determine whether token is a path candidate
        is_candidate = (
            candidate.startswith(("/", "./", "../", "~/"))
            or "://" in candidate
            or candidate.startswith("file:")
        )
        if not is_candidate:
            return m.group(0)

        # Resolve path candidate
        p = candidate
        if p.startswith("file://"):
            p = p[len("file://"):]
        elif p.startswith("file:"):
            p = p[len("file:"):]

        if p.startswith("~"):
            p = os.path.expanduser(p)

        if not os.path.isabs(p):
            target_path = os.path.normpath(os.path.join(session_dir, p))
        else:
            target_path = p

        if path_is_under(target_path, session_dir):
            return m.group(0)
        return "[blocked-path]" + trailing_punct

    return _AT_TOKEN_RE.sub(_replace, prompt)


def _build_dsh_command(prompt: str) -> list:
    """Build the dsh argv: ``dsh --profile <profile> -- <prompt>``.

    The headless profile joins its positionals into one task, so the prompt is
    passed as a single positional preceded by ``--`` to prevent option injection.
    No continuation flag exists: headless always creates a fresh session.
    """
    return [config.dsh_binary_path, "--profile", config.dsh_profile, "--", prompt]


# ---------------------------------------------------------------------------
# dsh CLI execution
# ---------------------------------------------------------------------------

def clean_display(stdout_text: str) -> str:
    """Clean CLI stdout for WeChat display and strip file link targets."""
    display = clean_output(stdout_text) or EMPTY_REPLY
    return _strip_file_links(display)


async def run_dsh(prompt: str, user_id: str, timeout: int = None) -> tuple[str, list]:
    """Execute the dsh headless profile for a single user message.

    - Runs with cwd = per-user session dir (per-user workspace isolation)
    - Passes DSH_HOME explicitly (machine-wide, see _host_dsh_home)
    - Single-turn: no session resume; every call is a fresh dsh session
    - Extracts file artifacts from stdout, cleans ANSI/HTML from display text
    - Kills the process group on timeout and returns a friendly message

    Returns:
        tuple[str, list]: (cleaned_display_text, list_of_(name, abs_path)_artifacts)
    """
    if timeout is None:
        timeout = config.dsh_timeout

    global _warned_dsh_home_implicit
    if not is_dsh_home_explicit() and not _warned_dsh_home_implicit:
        logger.warning("未设 WECHATBRIDGE_DSH_HOME，复用宿主 ~/.dsh，宿主会话保留清理已禁用")
        _warned_dsh_home_implicit = True

    if len(prompt.encode("utf-8", errors="replace")) > _MAX_ARG_BYTES:
        logger.warning("Prompt too large for argv from user %s", user_id)
        return format_error(
            "消息过长",
            f"这条消息太长了（超过 {_MAX_ARG_BYTES // 1024}KB），请精简或分段发送。",
        ), []

    t0 = time.time()
    session_dir = ensure_session_dir(user_id)

    # Audit logging
    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning("[AUDIT] dangerous keyword in prompt from user=%s", user_id)

    # Preflight: machine-wide credentials must exist (same spirit as agy's
    # auth-token copy).  Log the real path; never echo it to WeChat users.
    cred_path = os.path.join(_host_dsh_home(), ".credentials.yaml")
    if not os.path.exists(cred_path):
        logger.warning("dsh credentials missing for user %s: %s", user_id, cred_path)
        return format_error(
            "未登录",
            "助手尚未登录或凭证失效，请联系管理员处理。",
        ), []

    first = is_first_message(session_dir, backend="dsh")

    safe_prompt = _sanitize_prompt_at_paths(prompt, session_dir)
    cmd = _build_dsh_command(safe_prompt)

    logger.info(
        "Running dsh for user %s (first=%s): %s",
        user_id, first, " ".join(cmd[:3]) + " ...",
    )


    process = None
    try:
        env = sanitize_env(session_dir)
        # sanitize_env 不会碰 DSH_*（非敏感名），必须显式接管：
        #  - DSH_HOME 指向机器级主目录（否则 HOME=session_dir 会让 dsh 解析到
        #    会话目录下、找不到 profile）
        #  - 剥离桥进程自身可能带有的 dsh 会话变量
        env["DSH_HOME"] = _host_dsh_home()
        for k in _DSH_SESSION_ENV_KEYS:
            env.pop(k, None)
        env["PAGER"] = "cat"
        env["CI"] = "true"
        env["NONINTERACTIVE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session_dir,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=float(timeout),
        )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        # Artifacts come from raw stdout (before clean_output)
        artifacts = extract_artifacts(stdout_text, cwd=session_dir)

        display = clean_display(stdout_text)

        if process.returncode != 0:
            logger.warning(
                "dsh exited with code %s for user %s: %.200s",
                process.returncode,
                user_id,
                stderr_text,
            )
            raw = stderr_text.removeprefix("dsh: ").strip() or stdout_text or "process exited abnormally"
            return format_cli_error(raw, backend="dsh"), []

        # Success path only — never mark on ❌/🔔 error/throttle bubbles
        if first and display != EMPTY_REPLY and not is_bridge_formatted_reply(display):
            mark_initialized(session_dir, backend="dsh")

        elapsed = time.time() - t0
        logger.info(
            "dsh done: user=%s elapsed=%.1fs artifacts=%d output=%d chars",
            user_id, elapsed, len(artifacts), len(display),
        )
        return display, artifacts

    except asyncio.TimeoutError:
        logger.warning(
            "dsh execution timed out after %ss for user %s",
            timeout,
            user_id,
        )
        await terminate_process(process, graceful=True)
        return format_error("处理超时", f"超过 {timeout} 秒未完成，已终止本次任务。"), []

    except asyncio.CancelledError:
        # 任务被取消（如重登录前排空）：必须杀掉子进程再传递取消
        await terminate_process(process, graceful=False)
        raise

    except Exception as e:
        logger.exception("Unexpected error running dsh: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        ), []


# ---------------------------------------------------------------------------
# Slash command support
# ---------------------------------------------------------------------------

def _cmd_help() -> str:
    """Build /help response listing dsh-supported slash commands."""
    lines = [
        "📋 **wechatbridge 支持指令 (dsh)** 📋",
        "",
        "**引擎说明**",
        "- dsh 为**单轮模式**：每次提问都会开启全新会话",
        "- `/backend <agy|grok|codex|dsh>` — 切换助手引擎",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — dsh 单轮模式无需重置（指令已接受）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "",
        "提示：其他 `/` 指令会直接交给助手处理。",
    ]
    return "\n".join(lines)


async def handle_dsh_slash_command(text: str, user_id: str) -> str | None:
    """Handle /-slash commands for the dsh backend.

    Classification (mirrors agy.py):
      A — implemented here (help, clear/new, backend)
      B — dangerous (exit, quit, logout) → rejected
      C — TUI panels → not supported on WeChat
      D — passthrough to dsh → returns None

    Returns:
        str: reply message for A/B/C classes
        None: for D class — the caller should pass the original text to run_dsh()
    """
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else text.lower()
    args = parts[1] if len(parts) > 1 else ""

    # --- B class: dangerous / rejected ---
    B_CMDS = frozenset({"/exit", "/quit", "/logout"})
    if cmd in B_CMDS:
        return "⛔ **该指令在微信端禁用** ⛔"

    # --- C class: TUI panels (not supported on WeChat) ---
    C_CMDS = frozenset({
        "/config", "/settings", "/context", "/diff", "/artifact", "/tasks",
        "/hooks", "/keybindings", "/permissions", "/statusline",
        "/copy", "/open", "/rename", "/fork", "/branch", "/rewind", "/undo",
        "/resume", "/switch", "/conversation", "/title", "/feedback",
        "/usage", "/quota", "/credits", "/skills",
    })
    if cmd in C_CMDS:
        return f"⚠️ **微信端不支持** ⚠️\n\n`{cmd}`"

    # --- A class: implemented commands ---
    if cmd == "/help":
        return _cmd_help()

    if cmd in ("/clear", "/new"):
        return "ℹ️ **dsh 为单轮模式** ℹ️\n\n每次提问都会开启全新会话，无需重置。"

    # v1: model / effort / mode / persona are not wired to dsh yet.
    # /agent and /backend are meta-commands handled in main.py.
    if cmd in (
        "/model", "/models", "/fast", "/planning",
        "/add-dir", "/agents", "/persona", "/mcp",
    ):
        return "ℹ️ **该指令当前不支持 dsh 引擎** ℹ️\n\n请用 `/backend` 切换到 agy / grok / codex。"

    # --- D class: passthrough to dsh (return None so caller runs run_dsh) ---
    return None
