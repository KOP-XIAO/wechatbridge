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
    sanitize_user_id, get_session_dir, is_first_message, mark_initialized,
    clean_output, load_prefs, save_prefs, is_dangerous, parse_model_effort,
    sanitize_env, terminate_process,
)

logger = logging.getLogger("grok_runner")


# ---------------------------------------------------------------------------
# Per-user .grok directory setup
# ---------------------------------------------------------------------------

def ensure_user_grok(user_id: str) -> str:
    """Ensure per-user .grok directory with auth credentials.

    Creates session/.grok/ for grok config, auth, and conversations.
    Copies global auth.json on first use.
    Returns session_dir path (for use as HOME when running grok).
    """
    session_dir = get_session_dir(user_id)
    grok_dir = os.path.join(session_dir, ".grok")
    os.makedirs(grok_dir, exist_ok=True)

    # Copy global auth.json if not yet present
    auth_src = os.path.expanduser("~/.grok/auth.json")
    auth_dst = os.path.join(grok_dir, "auth.json")
    if not os.path.exists(auth_dst) and os.path.exists(auth_src):
        try:
            shutil.copy(auth_src, auth_dst)
            os.chmod(auth_dst, 0o600)
        except OSError as e:
            logger.warning("Failed to copy auth.json for %s: %s", user_id, e)

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


# ---------------------------------------------------------------------------
# Artifact extraction from chat_history.jsonl
# ---------------------------------------------------------------------------

def _extract_grok_artifacts(session_dir: str, session_id: str) -> list:
    """Extract (name, abs_path) tuples from grok session chat_history.jsonl.

    grok stores sessions under $HOME/.grok/sessions/<url-encoded-cwd>/<session-id>/.
    The chat_history.jsonl contains structured tool_calls with file_path arguments
    from write/edit operations.

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


def _parse_grok_output(stdout_text: str, session_dir: str) -> tuple:
    """Parse grok JSON output into (display_text, artifacts).

    Handles both success JSON ({text, sessionId, ...}) and error JSON
    ({type: error, message: ...}). Falls back to plain text on parse failure.
    """
    if not stdout_text:
        return "(empty response)", []

    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError:
        # Non-JSON output — treat as plain text
        return clean_output(stdout_text) or "(empty response)", []

    if data.get("type") == "error":
        msg = data.get("message", "unknown grok error")
        logger.warning("grok error: %s", msg)
        return f"❌ **{msg}** ❌", []

    display = data.get("text", "")
    session_id = data.get("sessionId", "")

    artifacts = []
    if session_id:
        artifacts = _extract_grok_artifacts(session_dir, session_id)

    # Strip file:/// links from display (in case grok emits them)
    display = re.sub(
        r"\[([^\]]+)\]\(file:///[^)]+\)",
        r"[\1]",
        display,
    )

    return clean_output(display) or "(empty response)", artifacts


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

    t0 = time.time()
    session_dir = ensure_user_grok(user_id)

    # Audit logging
    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning("[AUDIT] dangerous keyword in prompt from user=%s", user_id)

    first = is_first_message(session_dir)
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

        display, artifacts = _parse_grok_output(stdout_text, session_dir)

        if process.returncode != 0 and not display:
            logger.warning(
                "grok exited with code %s for user %s: %.200s",
                process.returncode, user_id, stderr_text,
            )
            return clean_output(stderr_text) or "❌ **grok 执行失败** ❌", []

        if first:
            mark_initialized(session_dir)

        elapsed = time.time() - t0
        logger.info(
            "grok done: user=%s elapsed=%.1fs artifacts=%d output=%d chars",
            user_id, elapsed, len(artifacts), len(display),
        )
        return display, artifacts

    except asyncio.TimeoutError:
        logger.warning("grok execution timed out after %ss for user %s", timeout, user_id)
        await terminate_process(process, graceful=True)
        return "⏰ **处理超时** ⏰", []

    except Exception as e:
        logger.exception("Unexpected error running grok: %s", e)
        await terminate_process(process, graceful=False)
        return f"❌ **执行出错** ❌\n\n```\n{str(e)}\n```", []


async def _run_grok_subcommand(subcmd_args: list, user_id: str) -> str:
    """Run a grok subcommand (e.g., 'models', 'agent') and return cleaned output.

    Timeout is fixed at 30 seconds.
    Uses per-user session isolation matching run_grok.
    """
    session_dir = ensure_user_grok(user_id)
    cmd = [config.grok_binary_path] + subcmd_args
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
            return clean_output(stderr_text) if stderr_text else "❌ **终端指令执行失败** ❌"

        return clean_output(stdout_text) or "(empty response)"

    except asyncio.TimeoutError:
        return "❌ **指令超时** ❌"
    except Exception as e:
        logger.exception("Subcommand error: %s", e)
        return f"❌ **执行出错** ❌\n\n```\n{str(e)}\n```"


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
    if output.startswith("❌"):
        return "❌ **无法获取模型列表** ❌"

    models = _parse_grok_models(output)
    if not models:
        # Fallback: treat non-empty lines as model names
        models = [line.strip() for line in output.split("\n") if line.strip()]

    # Exact match
    if name in models:
        prefs = load_prefs(user_id)
        prefs["model"] = name
        _, embedded = parse_model_effort(name)
        if embedded:
            prefs.pop("effort", None)
        save_prefs(user_id, prefs)
        return f"✅ **模型已切换** ✅\n\n`{name}`"

    # Prefix match
    prefix_matches = [m for m in models if m.startswith(name)]
    if prefix_matches:
        matched = prefix_matches[0]
        prefs = load_prefs(user_id)
        prefs["model"] = matched
        _, embedded = parse_model_effort(matched)
        if embedded:
            prefs.pop("effort", None)
        save_prefs(user_id, prefs)
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
        "- `/backend <agy|grok>` — 切换后端 CLI",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — 重置对话（开始新会话）",
        "- `/fast` — 开启**快速模式**（低推理开销）",
        "- `/planning` — 开启 **planning 模式**",
        "",
        "**工具**",
        "- `/add-dir <路径>` — 添加工作目录（grok 后端暂不支持，仅记录）",
        "- `/agents` — 查看可用 agent",
        "",
        "**人格**",
        "- `/persona <内容>` — 设置你专属的人格文档（支持 show / clear / reset 子命令）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "",
        "提示：其他 `/` 指令会直接交给 grok 处理。",
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
        flag_path = os.path.join(session_dir, ".initialized")
        try:
            if os.path.exists(flag_path):
                os.remove(flag_path)
            return "✅ **对话已重置** ✅"
        except OSError as e:
            logger.error("Failed to clear session for %s: %s", user_id, e)
            return "❌ **重置失败** ❌"

    if cmd == "/fast":
        prefs = load_prefs(user_id)
        prefs["effort"] = "low"
        save_prefs(user_id, prefs)
        return "✅ **已开启 fast 模式** ✅"

    if cmd == "/planning":
        prefs = load_prefs(user_id)
        prefs["mode"] = "plan"
        save_prefs(user_id, prefs)
        return "✅ **已开启 planning 模式** ✅"

    if cmd == "/model":
        return await _cmd_model(args, user_id)

    if cmd == "/add-dir":
        path = args.strip()
        if not path:
            return "❌ **缺少参数** ❌\n\n`/add-dir <路径>`"
        prefs = load_prefs(user_id)
        dirs = prefs.get("add_dirs", [])
        if path not in dirs:
            dirs.append(path)
            prefs["add_dirs"] = dirs
            save_prefs(user_id, prefs)
        return f"✅ **已记录工作目录** ✅\n\n```\n{path}\n```\n\nℹ️ grok 后端暂不支持通过命令行传递额外目录。"

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
            "ℹ️ **MCP 工具使用引导** ℹ️\n\n"
            "grok 已配置 MCP server。\n\n"
            "使用方法：用自然语言描述调用，格式为：\n"
            "> 用 `<工具名>` 调用，参数 `<json>`\n\n"
            "示例：\n"
            "> 用 codegraph 的 search 工具搜 ctxmode"
        )

    if cmd == "/agent":
        if not config.enable_subagent:
            return "ℹ️ **该功能已禁用** ℹ️"
        if not args:
            return "❌ **缺少参数** ❌\n\n`/agent <名称> <任务>`"
        agent_parts = args.split(maxsplit=1)
        agent_name = agent_parts[0]
        agent_task = agent_parts[1] if len(agent_parts) > 1 else ""
        crafted = f"请用 invoke_subagent 调用 agent {agent_name} 执行任务：{agent_task}"
        logger.info("Agent subcmd: user=%s agent=%s task=%.100s", user_id, agent_name, agent_task)
        result_text, _ = await run_grok(crafted, user_id)
        return result_text

    # --- D class: passthrough to grok (return None so caller runs run_grok) ---
    return None
