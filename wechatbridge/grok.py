"""grok (Grok Build CLI) runner with per-user session isolation.

Mirrors agy.py's interface: run_grok(), handle_grok_slash_command(),
ensure_user_grok(), get_session_dir() — all imported from runner_common.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
import urllib.parse

from .config import config
from .runner_common import (
    sanitize_user_id, get_session_dir, ensure_session_dir, is_first_message, mark_initialized, clear_initialized,
    clean_output, load_prefs, save_prefs, is_dangerous, parse_model_effort,
    sanitize_env, terminate_process, update_active_prefs,
    format_error, format_cli_error, is_bridge_formatted_reply, EMPTY_REPLY, validate_add_dir,
)

logger = logging.getLogger("grok_runner")

# execve 单参数上限（Linux MAX_ARG_STRLEN = 128KB），留安全余量
_MAX_ARG_BYTES = 120 * 1024


# ---------------------------------------------------------------------------
# Per-user .grok directory setup
# ---------------------------------------------------------------------------

def _host_grok_dir() -> str:
    """Host-level ~/.grok (bridge process home), not the per-user session HOME.

    Auth/login is machine-wide; conversation state stays under the session dir.
    Override with WECHATBRIDGE_HOST_HOME if the service home is not the login home.
    """
    host_home = os.environ.get("WECHATBRIDGE_HOST_HOME") or os.path.expanduser("~")
    return os.path.join(host_home, ".grok")


def _sync_grok_auth(grok_dir: str) -> bool:
    """Make session .grok/auth.json track the host login credentials.

    Bug fixed: we used to copy auth.json only once. Token refresh updates the
    host ~/.grok/auth.json, while the session kept a stale/missing copy →
    false "Not signed in" even when host login is still valid.

    Strategy: symlink session auth.json → host auth.json (shared refresh).
    Fall back to copy2 if symlink is not possible.
    Returns True if credentials are available for the child process.
    """
    auth_src = os.path.join(_host_grok_dir(), "auth.json")
    auth_dst = os.path.join(grok_dir, "auth.json")

    if not os.path.isfile(auth_src):
        logger.warning("Host grok auth missing: %s", auth_src)
        return False

    try:
        src_real = os.path.realpath(auth_src)
        if os.path.islink(auth_dst) or os.path.exists(auth_dst):
            try:
                if os.path.realpath(auth_dst) == src_real and os.path.isfile(auth_dst):
                    return True
            except OSError:
                pass
            try:
                os.unlink(auth_dst)
            except OSError:
                # Windows or busy file — try overwrite via copy below
                pass

        try:
            os.symlink(src_real, auth_dst)
            logger.info("Linked session auth.json -> %s", src_real)
            return True
        except OSError as e:
            logger.warning("symlink auth.json failed (%s), falling back to copy", e)
            shutil.copy2(auth_src, auth_dst)
            os.chmod(auth_dst, 0o600)
            return True
    except OSError as e:
        logger.warning("Failed to sync auth.json into %s: %s", grok_dir, e)
        return False


def ensure_user_grok(user_id: str) -> str:
    """Ensure per-user .grok directory with auth credentials.

    Creates session/.grok/ for grok config and conversations.
    Always syncs host auth.json (symlink preferred) so login state matches
    the machine-level `grok login`, not a stale one-shot copy.
    Returns session_dir path (for use as HOME when running grok).
    """
    session_dir = ensure_session_dir(user_id)
    grok_dir = os.path.join(session_dir, ".grok")
    os.makedirs(grok_dir, exist_ok=True)
    try:
        os.chmod(grok_dir, 0o700)
    except OSError:
        pass

    _sync_grok_auth(grok_dir)

    return session_dir


# ---------------------------------------------------------------------------
# Persona persistence (via --rules injection)
# ---------------------------------------------------------------------------

def _persona_path(session_dir: str) -> str:
    return os.path.join(session_dir, "grok_persona.txt")


def _read_persona(session_dir: str) -> str:
    """Read persona content for --rules injection. Returns empty string if none."""
    path = _persona_path(session_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def handle_grok_persona(args: str, user_id: str) -> str:
    """Handle /persona command for grok backend.

    Stores persona text in grok_persona.txt, injected via --rules at run time.
    Subcommands: set <content>, show, clear, reset (same as clear for grok).
    """
    session_dir = get_session_dir(user_id)
    persona_path = _persona_path(session_dir)

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    # set or implicit content
    if subcmd == "set" and rest:
        try:
            with open(persona_path, "w", encoding="utf-8") as f:
                f.write(rest)
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"
    elif subcmd and subcmd not in ("show", "clear", "reset", "set"):
        # No subcommand → treat whole args as content
        try:
            with open(persona_path, "w", encoding="utf-8") as f:
                f.write(args.strip())
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"

    # show
    if subcmd == "show":
        if not os.path.exists(persona_path):
            return "（未设置人格文档）"
        try:
            with open(persona_path, "r", encoding="utf-8") as f:
                val = f.read()
            if len(val) > 1500:
                val = val[:1500] + "\n\n（已截断至前1500字符）"
            return val or "（空文档）"
        except OSError as e:
            logger.error("Failed to read persona for %s: %s", user_id, e)
            return "❌ **读取人格文档失败** ❌"

    # clear
    if subcmd == "clear":
        if os.path.exists(persona_path):
            try:
                os.remove(persona_path)
                return "✅ **人格文档已清除** ✅"
            except OSError as e:
                logger.error("Failed to clear persona for %s: %s", user_id, e)
                return "❌ **清除人格文档失败** ❌"
        return "ℹ️ **本就无人格文档** ℹ️"

    # reset (grok has no global default persona; same as clear)
    if subcmd == "reset":
        if os.path.exists(persona_path):
            try:
                os.remove(persona_path)
                return "✅ **人格已重置** ✅"
            except OSError as e:
                logger.error("Failed to reset persona for %s: %s", user_id, e)
                return "❌ **重置人格文档失败** ❌"
        return "ℹ️ **本就无人格文档** ℹ️"

    # empty args
    return "📋 **/persona 用法** 📋\n\n- `/persona <内容>` 设置\n- `/persona show` 查看\n- `/persona clear` 清除\n- `/persona reset` 重置"


# ---------------------------------------------------------------------------
# Command builder (pure function for testability)
# ---------------------------------------------------------------------------

def _build_grok_command(prompt: str, prefs: dict, first: bool, persona_content: str = "") -> list:
    """Build grok CLI argv list. Pure function — does not execute.

    Flag mapping (agy → grok):
      --dangerously-skip-permissions → --always-approve
      --effort                       → --reasoning-effort
      --mode plan                    → --permission-mode plan
      -c                             → --continue
      (persona via GEMINI.md)        → --rules <content>
    """
    cmd = [config.grok_binary_path, "--output-format", "json"]

    mode = prefs.get("mode", "")
    if mode == "plan":
        cmd += ["--permission-mode", "plan"]
    else:
        cmd += ["--always-approve"]

    model = prefs.get("model", "")
    effort = prefs.get("effort", "")
    if model:
        base_model, embedded_effort = parse_model_effort(model)
        if embedded_effort and effort:
            cmd += ["--model", base_model, "--reasoning-effort", effort]
        elif embedded_effort:
            cmd += ["--model", model]
        else:
            cmd += ["--model", model]
            if effort:
                cmd += ["--reasoning-effort", effort]
    elif effort:
        cmd += ["--reasoning-effort", effort]

    # Persona injection via --rules
    if persona_content:
        cmd += ["--rules", persona_content]

    # Session continuation
    if not first:
        cmd += ["--continue"]

    cmd += ["-p", prompt]
    return cmd


def _has_grok_session(session_dir: str) -> bool:
    """Check if a grok session exists for this cwd (for --continue safety).

    grok --continue looks for the most recent session under
    .grok/sessions/<url-encoded-cwd>/.  If that directory is empty or
    missing, --continue will fail with "No session found".
    """
    grok_sessions = os.path.join(session_dir, ".grok", "sessions")
    cwd_encoded = urllib.parse.quote(session_dir, safe="")
    cwd_dir = os.path.join(grok_sessions, cwd_encoded)
    if not os.path.isdir(cwd_dir):
        return False
    try:
        for session_name in os.listdir(cwd_dir):
            session_path = os.path.join(cwd_dir, session_name)
            if os.path.isdir(session_path):
                try:
                    with os.scandir(session_path) as it:
                        if any(it):
                            return True
                except OSError:
                    pass
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Artifact extraction from chat_history.jsonl
# ---------------------------------------------------------------------------

def _extract_grok_artifacts(session_dir: str, session_id: str, since: float = 0.0) -> list:
    """Extract (name, abs_path) tuples from grok session chat_history.jsonl.

    grok stores sessions under $HOME/.grok/sessions/<url-encoded-cwd>/<session-id>/.
    The chat_history.jsonl contains structured tool_calls with file_path arguments
    from write/edit operations.

    ``since``: 只收录 mtime >= since 的文件——chat_history.jsonl 跨轮累积，
    不过滤的话 --continue 会话每轮都会把历史文件重发一遍。

    Falls back to empty list on any error (never blocks text reply).
    """
    grok_sessions = os.path.join(session_dir, ".grok", "sessions")
    cwd_encoded = urllib.parse.quote(session_dir, safe="")
    session_path = os.path.join(grok_sessions, cwd_encoded, session_id)
    chat_history = os.path.join(session_path, "chat_history.jsonl")

    if not os.path.exists(chat_history):
        logger.debug("No chat_history.jsonl at %s", chat_history)
        return []

    artifacts = []
    seen = set()
    try:
        with open(chat_history, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "assistant" and d.get("tool_calls"):
                    for tc in d.get("tool_calls", []):
                        name = tc.get("name", "")
                        args = tc.get("arguments", "")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                continue
                        if name in ("write", "edit", "str_replace") and isinstance(args, dict):
                            fp = args.get("file_path", "")
                            if fp and os.path.isabs(fp):
                                # 只收录本轮运行期间新写/修改的文件
                                try:
                                    if since and os.path.getmtime(fp) < since - 2.0:
                                        continue
                                except OSError:
                                    continue  # 文件已不存在，无需回传
                                art_name = os.path.basename(fp)
                                key = (art_name, fp)
                                if key not in seen:
                                    seen.add(key)
                                    artifacts.append(key)
    except OSError as e:
        logger.warning("Failed to read chat_history.jsonl: %s", e)

    if artifacts:
        logger.debug("Extracted %d grok artifacts: %s", len(artifacts), [n for n, _ in artifacts[:3]])
    return artifacts


def _parse_grok_output(stdout_text: str, session_dir: str, since: float = 0.0) -> tuple:
    """Parse grok JSON output into (display_text, artifacts).

    Handles both success JSON ({text, sessionId, ...}) and error JSON
    ({type: error, message: ...}). Falls back to plain text on parse failure.
    """
    if not stdout_text:
        return EMPTY_REPLY, []

    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError:
        # Non-JSON output — treat as plain text
        return clean_output(stdout_text) or EMPTY_REPLY, []

    if data.get("type") == "error":
        msg = data.get("message", "unknown grok error")
        logger.warning("grok error: %s", msg)
        return format_cli_error(msg, backend="grok"), []

    display = data.get("text", "")
    session_id = data.get("sessionId", "")

    artifacts = []
    if session_id:
        artifacts = _extract_grok_artifacts(session_dir, session_id, since=since)

    # Strip file:/// links from display (in case grok emits them)
    display = re.sub(
        r"\[([^\]]+)\]\(file:///[^)]+\)",
        r"[\1]",
        display,
    )

    return clean_output(display) or EMPTY_REPLY, artifacts


# ---------------------------------------------------------------------------
# grok CLI execution
# ---------------------------------------------------------------------------

async def run_grok(prompt: str, user_id: str, timeout: int = None) -> tuple:
    """Execute grok CLI for a given user message.

    Mirrors agy.run_agy() interface.
    Returns (cleaned_display_text, list_of_(name, abs_path)_artifacts).
    """
    if timeout is None:
        timeout = config.agy_timeout

    # argv 单参数上限约 128KB（MAX_ARG_STRLEN），超长 prompt 直接拒绝，避免 E2BIG
    if len(prompt.encode("utf-8", errors="replace")) > _MAX_ARG_BYTES:
        logger.warning("Prompt too large for argv from user %s", user_id)
        return format_error(
            "消息过长",
            f"这条消息太长了（超过 {_MAX_ARG_BYTES // 1024}KB），请精简或分段发送。",
        ), []

    t0 = time.time()
    session_dir = ensure_user_grok(user_id)

    # Audit logging
    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning("[AUDIT] dangerous keyword in prompt from user=%s", user_id)

    first = is_first_message(session_dir, backend="grok")

    # Safety: even if .initialized.grok exists, verify a real grok session
    # is on disk.  Sessions can be cleaned by TTL or expire on the grok side
    # while the flag remains, causing --continue to fail.
    if not first and not _has_grok_session(session_dir):
        logger.info(
            "grok session missing despite .initialized.grok for %s, "
            "treating as first message",
            user_id,
        )
        first = True
    prefs = load_prefs(user_id)
    persona_content = _read_persona(session_dir)
    cmd = _build_grok_command(prompt, prefs, first, persona_content)

    if first:
        logger.info("First message for user %s, running: grok -p ...", user_id)
    else:
        logger.info("Continuing conversation for user %s, running: grok --continue -p ...", user_id)

    process = None
    try:
        env = sanitize_env(session_dir)
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

        display, artifacts = _parse_grok_output(stdout_text, session_dir, since=t0)

        # Failure detection: 非零退出一律视为失败（与 agy 行为对齐）；
        # 零退出但 stdout 是已格式化的错误/通知气泡（❌ 或 🔔，含限流）也算失败。
        # 禁止仅靠 startswith("❌")——🔔 限流气泡也必须算 failed，且不得二次 format。
        failed = process.returncode != 0 or (
            isinstance(display, str) and is_bridge_formatted_reply(display)
        )
        if process.returncode != 0:
            logger.warning(
                "grok exited with code %s for user %s: %.200s",
                process.returncode, user_id, stderr_text,
            )
            artifacts = []
            # 已是 format_error|format_notice 气泡时禁止再 format_cli_error，
            # 否则 🔔 限流文案会被洗成 ❌ 执行失败，A 防护失效。
            if not (isinstance(display, str) and is_bridge_formatted_reply(display)):
                raw_err = stderr_text or ("" if display == EMPTY_REPLY else display) or "process exited abnormally"
                display = format_cli_error(raw_err, backend="grok")

        # Fallback: if --continue failed because no session was found,
        # retry once without --continue (fresh session).
        if failed and not first:
            raw_err_check = (stderr_text or (display if isinstance(display, str) else "") or "").lower()
            if "no session found" in raw_err_check:
                logger.warning(
                    "grok --continue failed (no session) for %s, "
                    "retrying without --continue",
                    user_id,
                )
                clear_initialized(session_dir, backend="grok")
                retry_cmd = _build_grok_command(prompt, prefs, True, persona_content)
                retry_process = await asyncio.create_subprocess_exec(
                    *retry_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=session_dir,
                    env=env,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                try:
                    r_stdout, r_stderr = await asyncio.wait_for(
                        retry_process.communicate(),
                        timeout=float(timeout),
                    )
                except asyncio.TimeoutError:
                    await terminate_process(retry_process, graceful=True)
                    return format_error("处理超时", f"超过 {timeout} 秒未完成，已终止本次任务。"), []
                except (asyncio.CancelledError, Exception):
                    await terminate_process(retry_process, graceful=False)
                    raise

                r_stdout_text = r_stdout.decode("utf-8", errors="replace").strip()
                r_stderr_text = r_stderr.decode("utf-8", errors="replace").strip()
                r_display, r_artifacts = _parse_grok_output(r_stdout_text, session_dir, since=t0)
                r_failed = retry_process.returncode != 0 or (
                    isinstance(r_display, str) and is_bridge_formatted_reply(r_display)
                )
                if retry_process.returncode != 0:
                    logger.warning(
                        "grok retry exited with code %s for user %s: %.200s",
                        retry_process.returncode, user_id, r_stderr_text,
                    )
                    r_artifacts = []
                    if not (isinstance(r_display, str) and is_bridge_formatted_reply(r_display)):
                        raw_err = r_stderr_text or ("" if r_display == EMPTY_REPLY else r_display) or "process exited abnormally"
                        r_display = format_cli_error(raw_err, backend="grok")
                if not r_failed:
                    mark_initialized(session_dir, backend="grok")
                elapsed = time.time() - t0
                logger.info(
                    "grok retry done: user=%s elapsed=%.1fs artifacts=%d output=%d chars failed=%s",
                    user_id, elapsed, len(r_artifacts), len(r_display), r_failed,
                )
                return r_display, r_artifacts

        # Only mark session initialized on a real successful reply
        # (never on 🔔 限流 / ❌ 错误气泡，即使 CLI 零退出)
        if first and not failed:
            mark_initialized(session_dir, backend="grok")

        elapsed = time.time() - t0
        logger.info(
            "grok done: user=%s elapsed=%.1fs artifacts=%d output=%d chars failed=%s",
            user_id, elapsed, len(artifacts), len(display), failed,
        )
        return display, artifacts

    except asyncio.TimeoutError:
        logger.warning("grok execution timed out after %ss for user %s", timeout, user_id)
        await terminate_process(process, graceful=True)
        return format_error("处理超时", f"超过 {timeout} 秒未完成，已终止本次任务。"), []

    except asyncio.CancelledError:
        # 任务被取消（如重登录前排空）：必须杀掉子进程再传递取消
        await terminate_process(process, graceful=False)
        raise

    except Exception as e:
        logger.exception("Unexpected error running grok: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        ), []


async def _run_grok_subcommand(subcmd_args: list, user_id: str) -> str:
    """Run a grok subcommand (e.g., 'models', 'agent') and return cleaned output.

    Timeout is fixed at 30 seconds.
    Uses per-user session isolation matching run_grok.
    """
    session_dir = ensure_user_grok(user_id)
    cmd = [config.grok_binary_path] + subcmd_args
    process = None
    try:
        env = sanitize_env(session_dir)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session_dir,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=30.0
        )
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            logger.warning("grok %s exited with code %s", " ".join(subcmd_args), process.returncode)
            # Prefer stdout (often JSON error) then stderr
            raw = stdout_text or stderr_text or "终端指令执行失败"
            try:
                data = json.loads(stdout_text) if stdout_text else None
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and data.get("type") == "error":
                return format_cli_error(data.get("message") or raw, backend="grok")
            return format_cli_error(raw, backend="grok")

        return clean_output(stdout_text) or EMPTY_REPLY

    except asyncio.TimeoutError:
        # 超时必须回收子进程，否则挂死的查询进程成为孤儿
        await terminate_process(process, graceful=True)
        return format_error("查询超时", "查询超时，请稍后再试。")
    except asyncio.CancelledError:
        await terminate_process(process, graceful=False)
        raise
    except Exception as e:
        logger.exception("Subcommand error: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        )


# ---------------------------------------------------------------------------
# Slash command support
# ---------------------------------------------------------------------------

def _parse_grok_models(output: str) -> list:
    """Parse grok models output into a list of model names.

    grok output format:
      Available models:
        * grok-4.5 (default)
        * grok-4.5-mini
    """
    models = []
    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("* "):
            name = line[2:].split()[0].split("(")[0].strip()
            if name:
                models.append(name)
    return models


async def _cmd_model(args: str, user_id: str) -> str:
    """Handle /model <name>: validate against grok models list, then save."""
    name = args.strip()
    if not name:
        return "❌ **缺少参数** ❌\n\n`/model <名称>`"

    output = await _run_grok_subcommand(["models"], user_id)
    # 认 ❌/🔔 格式化错误气泡与限流通知，勿把中文错误当模型列表 parse
    if is_bridge_formatted_reply(output):
        return "❌ **无法获取模型列表** ❌"

    models = _parse_grok_models(output)
    if not models:
        # Fallback: treat non-empty lines as model names
        models = [line.strip() for line in output.split("\n") if line.strip()]

    # Exact match
    if name in models:
        _, embedded = parse_model_effort(name)
        if embedded:
            update_active_prefs(user_id, model=name, effort="")
        else:
            update_active_prefs(user_id, model=name)
        return f"✅ **模型已切换** ✅\n\n`{name}`"

    # Prefix match
    prefix_matches = [m for m in models if m.startswith(name)]
    if prefix_matches:
        matched = prefix_matches[0]
        _, embedded = parse_model_effort(matched)
        if embedded:
            update_active_prefs(user_id, model=matched, effort="")
        else:
            update_active_prefs(user_id, model=matched)
        return f"✅ **模型已切换** ✅\n\n`{matched}`"

    return f"❌ **模型不存在** ❌\n\n`{name}`"


def _cmd_help() -> str:
    """Build /help response for grok backend."""
    lines = [
        "📋 **wechatbridge 支持指令 (grok)** 📋",
        "",
        "**模型控制**",
        "- `/model <名称>` — 切换模型（用 `/models` 查看可用列表）",
        "- `/models` — 查看可用模型列表",
        "- `/backend <agy|grok|codex>` — 切换助手引擎",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — 重置对话（开始新会话）",
        "- `/fast` — 开启**快速模式**（回答更快，思考更少）",
        "- `/planning` — 开启**规划模式**（先想清楚再动手）",
        "",
        "**工具**",
        "- `/add-dir <路径>` — 添加工作目录（当前 grok 引擎暂不支持，仅记录）",
        "- `/agents` — 查看可用助手",
        "",
        "**人格**",
        "- `/persona <内容>` — 设置你专属的人格文档（另有 show / clear / reset）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "",
        "提示：其他 `/` 指令会直接交给助手处理。",
    ]
    return "\n".join(lines)


async def handle_grok_slash_command(text: str, user_id: str) -> str | None:
    """Handle /-slash commands for grok backend.

    Returns str for A/B/C classes, None for D class (passthrough to run_grok).
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
        session_dir = get_session_dir(user_id)
        clear_initialized(session_dir, backend="grok")
        return "✅ **对话已重置** ✅"

    if cmd == "/fast":
        update_active_prefs(user_id, effort="low")
        return "✅ **已开启快速模式** ✅"

    if cmd == "/planning":
        update_active_prefs(user_id, mode="plan")
        return "✅ **已开启规划模式** ✅"

    if cmd == "/model":
        return await _cmd_model(args, user_id)

    if cmd == "/add-dir":
        path = args.strip()
        if not path:
            return "❌ **缺少参数** ❌\n\n`/add-dir <路径>`"
        ok, result = validate_add_dir(path, user_id)
        if not ok:
            return f"❌ **目录不允许** ❌\n\n{result}"
        resolved = result
        prefs = load_prefs(user_id)
        dirs = prefs.get("add_dirs", [])
        if resolved not in dirs:
            dirs.append(resolved)
            prefs["add_dirs"] = dirs
            save_prefs(user_id, prefs)
        return (
            f"✅ **已记录工作目录** ✅\n\n```\n{resolved}\n```\n\n"
            "ℹ️ 当前引擎暂不支持额外工作目录，路径已记下备用。"
        )

    if cmd == "/agents":
        output = await _run_grok_subcommand(["agent"], user_id)
        return output

    if cmd == "/models":
        return await _run_grok_subcommand(["models"], user_id)

    if cmd == "/persona":
        return handle_grok_persona(args, user_id)

    # --- MCP & Subagent ---
    if cmd == "/mcp":
        if not config.enable_mcp:
            return "ℹ️ **该功能已禁用** ℹ️"
        return (
            "ℹ️ **扩展工具说明** ℹ️\n\n"
            "可以直接用自然语言让助手调用已配置的扩展工具。\n\n"
            "示例：\n"
            "> 用 codegraph 的 search 搜一下 ctxmode\n"
            "> 帮我查一下这个项目里 xxx 怎么实现的"
        )

    # /agent 已上移到 main.py 统一处理（必须经过危险确认门，不能再绕过）

    # --- D class: passthrough to grok (return None so caller runs run_grok) ---
    return None
