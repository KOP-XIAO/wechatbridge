"""
wechatbridge Main Entry Point.
Active iLink client that receives WeChat messages and responds via CLI backends.
Architecture: WeChat ClawBot(iLink) <-> wechatbridge(Python) <-> agy/grok CLI
"""

import argparse
import asyncio
import base64
import logging
import os
import sys
import time
import uuid
from collections import OrderedDict
from io import StringIO

from . import __version__
from .config import config, ensure_runtime_dirs
from .ilink import ILinkClient
from .runner_common import (
    clean_session_media,
    format_error,
    format_model_label,
    get_session_dir,
    is_dangerous,
    load_prefs,
    path_is_under,
    save_prefs,
    switch_backend_prefs,
)
from .update_check import update_check_loop, maybe_notify_admin, format_update_hint

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wechatbridge")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Pending dangerous prompt confirmations (user_id -> {prompt, expire_at, context_token})
pending_confirms: dict = {}


# Slash 处理器内部信号：该指令已完整处理（如 /agent 进入确认流程），调用方直接 return
_HANDLED = object()

# 重登录/关闭前等待在途消息任务的最长时间
_DRAIN_TIMEOUT_S = 90.0

# 后台任务强引用集合（事件循环只持弱引用，防止任务被 GC 提前回收）
_background_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    """create_task + 强引用跟踪，任务结束自动移除。"""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


# 已见消息 ID（LRU，上限 1000）——服务端重投/重启重放时跳过重复处理
_seen_msg_ids: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MSG_IDS_CAP = 1000


# 去重键字段优先级，对齐官方 WeixinMessage schema（Tencent/openclaw-weixin types.ts）：
#   message_id(数值) > client_id > item_list[0].msg_id > seq
def _msg_dedup_key(msg: dict) -> str | None:
    """从入站消息提取去重键；服务端不下发任何 id 字段时返回 None（无法安全去重）。"""
    for field in ("message_id", "client_id"):
        v = msg.get(field)
        if v:
            return f"{field}:{v}"
    items = msg.get("item_list")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        v = items[0].get("msg_id")
        if v:
            return f"item_msg_id:{v}"
    v = msg.get("seq")
    if v:
        return f"seq:{v}"
    return None


_dedup_field_announced = False


def _is_duplicate_msg(msg: dict) -> bool:
    global _dedup_field_announced
    key = _msg_dedup_key(msg)
    if not _dedup_field_announced:
        _dedup_field_announced = True
        if key:
            logger.info("消息去重已启用，使用字段: %s", key.split(":", 1)[0])
        else:
            logger.info("服务端未下发消息 id 字段，去重停用（依赖服务端游标）")
    if key is None:
        return False
    if key in _seen_msg_ids:
        return True
    _seen_msg_ids[key] = None
    _seen_msg_ids.move_to_end(key)
    while len(_seen_msg_ids) > _SEEN_MSG_IDS_CAP:
        _seen_msg_ids.popitem(last=False)
    return False


# ---------------------------------------------------------------------------
# Backend dispatcher — routes to agy or grok based on per-user preference
# ---------------------------------------------------------------------------

def _get_backend(user_id: str) -> str:
    """Get the active backend for a user (from prefs, fallback to global config)."""
    prefs = load_prefs(user_id)
    return prefs.get("backend", config.backend)


async def _run_llm(prompt: str, user_id: str) -> tuple[str, list]:
    """Dispatch prompt to the active backend's run function."""
    backend = _get_backend(user_id)
    if backend == "grok":
        from .grok import run_grok
        return await run_grok(prompt, user_id)
    else:
        from .agy import run_agy
        return await run_agy(prompt, user_id)


async def _handle_slash(client: ILinkClient, text: str, user_id: str, context_token: str):
    """Dispatch slash command to the active backend's handler.

    /backend 和 /agent 是元指令，在这里处理，不下发给后端。

    返回值约定：
      str      — 直接作为回复文本
      tuple    — (reply, artifacts)，经确认门后的执行结果
      None     — D 类透传，调用方应把原文交给 gate_and_run
      _HANDLED — 已完整处理（如 /agent 进入确认流程），调用方直接 return
    """
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    # /backend is a meta-command — switch CLI backend per user
    if cmd == "/backend":
        return _cmd_backend(args, user_id)

    # /agent 也在这里处理：统一走 gate_and_run 确认门，不能绕开 is_dangerous
    if cmd == "/agent":
        return await _cmd_agent(client, args, user_id, context_token)

    if cmd == "/version":
        return (
            f"📦 **版本信息** 📦\n\n当前版本: `{__version__}`\n"
            f"实例: `{config.instance}`  后端: `{_get_backend(user_id)}`"
        ) + format_update_hint()

    backend = _get_backend(user_id)
    if backend == "grok":
        from .grok import handle_grok_slash_command
        return await handle_grok_slash_command(text, user_id)
    else:
        from .agy import handle_slash_command
        return await handle_slash_command(text, user_id)


def _cmd_backend(args: str, user_id: str) -> str:
    """Handle /backend <agy|grok> — switch CLI backend per user.

    Switching restores that backend's remembered model/effort/mode (or empty
    project default on first visit), and resets the conversation so the new
    backend starts a fresh session.
    """
    name = args.strip().lower()
    if not name:
        prefs = load_prefs(user_id)
        current = prefs.get("backend", config.backend)
        model_label = format_model_label(prefs.get("model", ""))
        return (
            f"📋 **当前后端** 📋\n\n`{current}`\n"
            f"模型: `{model_label}`\n\n"
            "用法: `/backend agy` 或 `/backend grok`"
        )
    if name not in ("agy", "grok"):
        return "❌ **未知后端** ❌\n\n支持: `agy` / `grok`\n\n`/backend agy` 或 `/backend grok`"
    prefs = load_prefs(user_id)
    old, new = switch_backend_prefs(prefs, name)
    save_prefs(user_id, prefs)
    model_label = format_model_label(prefs.get("model", ""))
    # Reset session so new backend starts fresh (only when actually changed)
    if old != new:
        session_dir = get_session_dir(user_id)
        flag = os.path.join(session_dir, ".initialized")
        if os.path.exists(flag):
            os.remove(flag)
        return (
            f"✅ **后端已切换** ✅\n\n"
            f"`{old}` → `{new}`\n"
            f"模型: `{model_label}`\n\n"
            "⚠️ 对话已重置，新后端将开始新会话。"
        )
    return (
        f"📋 **当前后端** 📋\n\n`{name}`（未变化）\n"
        f"模型: `{model_label}`"
    )


async def _cmd_agent(client: ILinkClient, args: str, user_id: str, context_token: str):
    """Handle /agent <名称> <任务> — 调用子代理执行任务。

    必须经过 gate_and_run 的危险确认门（历史上后端各自实现时绕过了该检查）。
    """
    if not config.enable_subagent:
        return "ℹ️ **该功能已禁用** ℹ️"
    if not args.strip():
        return "❌ **缺少参数** ❌\n\n`/agent <名称> <任务>`"
    agent_parts = args.split(maxsplit=1)
    agent_name = agent_parts[0]
    agent_task = agent_parts[1] if len(agent_parts) > 1 else ""
    crafted = f"请用 invoke_subagent 调用 agent {agent_name} 执行任务：{agent_task}"
    logger.info("Agent subcmd: user=%s agent=%s task=%.100s", user_id, agent_name, agent_task)
    result = await gate_and_run(client, user_id, context_token, crafted)
    if result is None:
        return _HANDLED  # 已进入危险确认流程
    return result  # (reply, artifacts)


# ---------------------------------------------------------------------------
# Image file extension detection
# ---------------------------------------------------------------------------

def _detect_image_ext(data: bytes) -> str:
    """Detect image file extension from magic bytes."""
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] in (b"GIF8",):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "bin"


# ---------------------------------------------------------------------------
# QR code login flow
# ---------------------------------------------------------------------------
async def login_flow(client: ILinkClient) -> bool:
    """Perform QR code login flow.  Returns True on success."""
    qrcode_str, qrcode_url = await client.get_qrcode()

    # Save QR code PNG from URL
    if qrcode_url:
        try:
            import qrcode as qrcode_lib_png

            qr = qrcode_lib_png.QRCode(border=2)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            im = qr.make_image()
            parent = os.path.dirname(os.path.abspath(config.qrcode_png_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            im.save(config.qrcode_png_path)
            try:
                os.chmod(config.qrcode_png_path, 0o600)
            except OSError:
                pass
            logger.info(
                "二维码图片已保存到 %s", config.qrcode_png_path
            )
        except Exception as e:
            logger.warning("保存二维码 PNG 失败: %s", e)

        # Write URL to file for external access (restrict permissions)
        try:
            parent = os.path.dirname(os.path.abspath(config.qrcode_url_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(config.qrcode_url_path, "w") as f:
                f.write(qrcode_url)
            os.chmod(config.qrcode_url_path, 0o600)
        except Exception as e:
            logger.warning("写入二维码 URL 文件失败: %s", e)

    logger.info(
        "请用手机微信扫描 %s 或下方二维码完成绑定",
        config.qrcode_png_path,
    )

    # Render ASCII QR code for terminal scanning
    try:
        import qrcode as qrcode_lib

        qr = qrcode_lib.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        buf = StringIO()
        qr.print_ascii(out=buf)
        ascii_art = buf.getvalue()
        logger.info("ASCII 二维码:\n%s", ascii_art)
    except Exception as e:
        logger.debug("无法渲染 ASCII 二维码: %s", e)

    logger.info("等待扫码...（超时 %d 秒）", config.qrcode_poll_timeout)

    try:
        bot_token, baseurl = await client.poll_qrcode_status(
            qrcode_str,
            timeout=config.qrcode_poll_timeout,
        )
        client.state.bot_token = bot_token
        client.state.baseurl = baseurl
        client.state.bound_at = int(time.time())
        client.state.save()
        logger.info("绑定成功！bot_token 已持久化")
        return True
    except TimeoutError:
        logger.error("扫码超时，退出")
        return False


async def gate_and_run(client, from_user, context_token, prompt) -> tuple[str, list] | None:
    """Check prompt with is_dangerous; if dangerous, ask for confirmation.

    Returns (reply, artifacts) on safe prompt, None if confirmation asked.
    """
    if is_dangerous(prompt):
        expire_at = time.time() + config.pending_confirm_ttl
        pending_confirms[from_user] = {
            "prompt": prompt,
            "expire_at": expire_at,
            "context_token": context_token,
        }
        await client.send_message(
            to_user_id=from_user,
            text=(
                f"⚠️ **危险操作确认** ⚠️\n\n```\n{prompt}\n```\n\n"
                f"- 回复 **{config.confirm_token}** → 执行\n"
                f"- 回复其他 → 取消"
            ),
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )
        logger.warning("[AUDIT] dangerous prompt pending confirmation: user=%s prompt=%.200s", from_user, prompt)
        return None
    return await _run_llm(prompt, from_user)


async def send_artifacts_back(client, from_user, context_token, artifacts) -> None:
    """Filter artifacts: only send back those under per-user session dir.

    For agy: artifacts under .gemini/antigravity-cli/scratch
    For grok: artifacts under session_dir (cwd where grok ran)
    """
    session_dir = get_session_dir(from_user)
    backend = _get_backend(from_user)
    if backend == "grok":
        # grok runs with cwd=session_dir, artifacts are under session_dir
        allowed_root = session_dir
    else:
        # agy writes to .gemini/antigravity-cli/scratch under session_dir
        allowed_root = os.path.join(session_dir, ".gemini", "antigravity-cli", "scratch")
    for art_name, art_path in artifacts:
        try:
            # realpath check blocks symlink escape outside allowed root
            if not path_is_under(art_path, allowed_root):
                logger.debug("skip non-scratch artifact: %s", art_path)
                continue
            if not os.path.isfile(os.path.realpath(art_path)):
                logger.warning("Artifact not found: %s", art_path)
                continue
            art_path = os.path.realpath(art_path)
            file_size = os.path.getsize(art_path)
            if file_size > config.max_outbound_file_bytes:
                size_mb = file_size / (1024 * 1024)
                await client.send_message(
                    to_user_id=from_user,
                    text=f"⚠️ **文件过大** ⚠️\n\n`{art_name}` {size_mb:.1f} MB\n已存：`{art_path}`",
                    context_token=context_token,
                    baseurl=client.state.baseurl,
                    bot_token=client.state.bot_token,
                )
                continue
            ok = await client.send_media(
                to_user_id=from_user,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
                context_token=context_token,
                path=art_path,
                caption="",
            )
            if ok:
                logger.info("Artifact sent: %s -> %s", art_name, from_user)
            else:
                logger.warning("Failed to send artifact: %s", art_name)
        except Exception as e:
            logger.exception("Error sending artifact %s: %s", art_name, e)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------
async def process_message(client: ILinkClient, msg: dict) -> None:
    """Process a single WeChat message.

    - Image messages (type==2 item): download, AES decrypt, detect ext, save,
      then run CLI with ``prompt @path`` for image recognition.
    - Text-only messages: original logic (slash interception + run_llm).

    Image messages bypass slash command interception; the text caption (if any)
    is used as the prompt, otherwise a default prompt is used.
    """
    from_user = msg.get("from_user_id", "")
    context_token = msg.get("context_token", "")
    item_list = msg.get("item_list", [])
    logger.debug(
        "process_message: from=%s msg_type=%d items=%d",
        from_user, msg.get("message_type", 0), len(item_list),
    )

    # Extract image media and text from item_list
    text = ""
    image_media = None
    file_media = None
    file_name = ""
    voice_text = ""
    has_voice = False
    for item in item_list:
        item_type = item.get("type")
        if item_type == 1 and not text:
            text_item = item.get("text_item", {})
            text = text_item.get("text", "")
        elif item_type == 2 and image_media is None:
            image_item = item.get("image_item", {})
            media = image_item.get("media", {})
            if media.get("encrypt_query_param") or media.get("full_url"):
                image_media = media
        elif item_type == 3 and not has_voice:
            # Voice: WeChat transcribes to text server-side (voice_item.text).
            # Only passthrough the text — no silk decode / no ASR (dmit 1c965Mi).
            voice_item = item.get("voice_item", {})
            voice_text = voice_item.get("text", "") or ""
            has_voice = True
        elif item_type == 4 and file_media is None:
            fi = item.get("file_item", {})
            media = fi.get("media", {})
            if media.get("encrypt_query_param") or media.get("full_url"):
                file_media = media
                file_name = fi.get("file_name", "")

    if not context_token:
        logger.warning(
            "Message from %s has no context_token, cannot reply", from_user
        )
        return

    # ---- Whitelist check (before any processing) ----
    if config.allowed_senders and from_user not in config.allowed_senders:
        await client.send_message(
            to_user_id=from_user,
            text="⛔ **未授权用户** ⛔\n联系管理员添加白名单。",
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )
        logger.warning("拒绝非白名单用户: %s", from_user)
        return

    # ---- Admin notification (update available, etc.) ----
    await maybe_notify_admin(client, from_user, context_token)

    # ---- Pending dangerous prompt confirmation ----
    # 确认匹配用文本或语音转写（语音消息也可以回确认口令）
    reply_text = text.strip() or voice_text.strip()
    cancelled_notice = ""
    pending = pending_confirms.get(from_user)
    if pending:
        expired = time.time() >= pending["expire_at"]
        if not expired and reply_text.lower() == config.confirm_token.lower():
            # User confirmed → run pending prompt, send reply, return
            logger.info("[AUDIT] user=%s confirmed dangerous prompt", from_user)
            # 先删再执行：执行异常也不能让陈旧 pending 被后续消息误触发
            del pending_confirms[from_user]
            if image_media or file_media:
                logger.info("确认回复携带媒体，媒体部分被忽略 from=%s", from_user)
            reply, artifacts = await _run_llm(pending["prompt"], from_user)
            # Send reply
            success = await client.send_message(
                to_user_id=from_user,
                text=reply,
                context_token=context_token,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
            )
            if success:
                logger.info("回复已发送到 %s", from_user)
            else:
                logger.warning("回复发送失败到 %s", from_user)
            # Send artifacts
            await send_artifacts_back(client, from_user, context_token, artifacts)
            return
        del pending_confirms[from_user]
        if expired:
            # Expired: don't reply, continue normal flow
            logger.info("[AUDIT] user=%s pending expired, continue normal flow", from_user)
        elif image_media or file_media:
            # 用户发来新媒体内容：取消待确认并继续处理本条消息（不静默丢弃）
            logger.info("[AUDIT] user=%s cancelled pending by sending new media", from_user)
            cancelled_notice = "🚫 已取消待确认的危险操作。\n\n"
        else:
            # User explicitly cancelled: reply cancelled, return
            logger.info("[AUDIT] user=%s cancelled dangerous prompt", from_user)
            await client.send_message(
                to_user_id=from_user,
                text="🚫 **已取消** 🚫",
                context_token=context_token,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
            )
            return

    # ---- Case 1: Message contains an image ----
    artifacts = []
    reply = ""
    if image_media:
        if not image_media.get("aes_key"):
            reply = format_error("图片无法处理", "缺少解密密钥，请重新发送图片。")
            logger.warning("图片缺少 aes_key from=%s", from_user)
        else:
            try:
                # Download CDN image and AES decrypt → plaintext bytes
                plain_bytes = await client.download_and_decrypt_media(image_media)

                # Detect extension from magic bytes
                ext = _detect_image_ext(plain_bytes)

                # Save to per-user session images directory
                images_dir = os.path.join(get_session_dir(from_user), "images")
                os.makedirs(images_dir, exist_ok=True)
                try:
                    os.chmod(images_dir, 0o700)
                except OSError:
                    pass
                save_path = os.path.join(images_dir, f"{uuid.uuid4().hex[:12]}.{ext}")
                with open(save_path, "wb") as f:
                    f.write(plain_bytes)

                logger.info(
                    "图片已保存 %s (%d bytes, ext=%s)",
                    save_path, len(plain_bytes), ext,
                )

                # Build prompt: user's caption if present, else default
                prompt = text.strip() if text.strip() else "请描述这张图片的内容"
                logger.info("识图 from=%s: %s @%s", from_user, prompt, save_path)
                result = await gate_and_run(client, from_user, context_token, f"{prompt} @{save_path}")
                if result is None:
                    return
                reply, artifacts = result

            except Exception as e:
                logger.exception("图片下载/解密失败: %s", e)
                # ValueError 是自定义的中文可读原因；其他异常（httpx 等）含内部 URL，不外发
                detail = str(e) if isinstance(e, ValueError) else "请重新发送图片。"
                reply = format_error("图片下载或解密失败", detail)

    # ---- Case 1.5: Message contains a file (non-image) ----
    elif file_media:
        if not file_media.get("aes_key"):
            reply = format_error("文件无法处理", "缺少解密密钥，请重新发送文件。")
            logger.warning("文件缺少 aes_key from=%s", from_user)
        else:
            try:
                plain_bytes = await client.download_and_decrypt_media(file_media)

                # Save to per-user session files directory
                files_dir = os.path.join(get_session_dir(from_user), "files")
                os.makedirs(files_dir, exist_ok=True)
                try:
                    os.chmod(files_dir, 0o700)
                except OSError:
                    pass
                # Preserve original extension from file_name (basename only)
                ext = os.path.splitext(os.path.basename(file_name or ""))[1]
                save_name = f"{uuid.uuid4().hex[:12]}{ext}" if ext else uuid.uuid4().hex[:12]
                save_path = os.path.join(files_dir, save_name)
                with open(save_path, "wb") as f:
                    f.write(plain_bytes)

                logger.info(
                    "文件已保存 %s (%d bytes)", save_path, len(plain_bytes),
                )

                prompt = text.strip() if text.strip() else "请分析这个文件"
                logger.info("文件分析 from=%s: %s @%s", from_user, prompt, save_path)
                result = await gate_and_run(client, from_user, context_token, f"{prompt} @{save_path}")
                if result is None:
                    return
                reply, artifacts = result

            except Exception as e:
                logger.exception("文件下载/解密失败: %s", e)
                # ValueError 是自定义的中文可读原因；其他异常（httpx 等）含内部 URL，不外发
                detail = str(e) if isinstance(e, ValueError) else "请重新发送文件。"
                reply = format_error("文件下载或解密失败", detail)

    # ---- Case 1.6: Voice message (text transcription passthrough) ----
    elif has_voice:
        if voice_text.strip():
            logger.info("语音转文字 from=%s: %.100s", from_user, voice_text.strip())
            result = await gate_and_run(client, from_user, context_token, voice_text.strip())
            if result is None:
                return
            reply, artifacts = result
        else:
            # WeChat failed to transcribe the voice → ask user to type.
            reply = "🤔 **听不清，请打字** 🤔"
            logger.info("语音未识别出文字 from=%s", from_user)

    # ---- Case 2: Text-only message (original logic) ----
    else:
        if not text:
            logger.debug("Skipping non-text message from %s", from_user)
            return

        logger.info("收到消息 from=%s: %.100s", from_user, text)

        # Slash command interception
        if text.startswith("/"):
            logger.info("Slash command from=%s: %.200s", from_user, text)
            handled = await _handle_slash(client, text, from_user, context_token)
            if handled is _HANDLED:
                return
            if handled is None:
                # D class: passthrough — run CLI normally
                result = await gate_and_run(client, from_user, context_token, text)
                if result is None:
                    return
                reply, artifacts = result
            elif isinstance(handled, tuple):
                # /agent 等元指令经确认门后的执行结果
                reply, artifacts = handled
            else:
                reply = handled
        else:
            result = await gate_and_run(client, from_user, context_token, text)
            if result is None:
                return
            reply, artifacts = result

    # ---- Send reply via iLink ----
    success = await client.send_message(
        to_user_id=from_user,
        text=cancelled_notice + reply,
        context_token=context_token,
        baseurl=client.state.baseurl,
        bot_token=client.state.bot_token,
    )

    if success:
        logger.info("回复已发送到 %s", from_user)
    else:
        logger.warning("回复发送失败到 %s", from_user)

    # ---- Send artifacts back to WeChat ----
    await send_artifacts_back(client, from_user, context_token, artifacts)


# ---------------------------------------------------------------------------
# Scratch TTL cleanup
# ---------------------------------------------------------------------------

def clean_scratch():
    """Remove old global scratch files and per-user session media."""
    scratch_dir = config.agy_scratch_dir
    if os.path.isdir(scratch_dir):
        now = time.time()
        cutoff = now - config.scratch_retention_days * 86400
        try:
            for name in os.listdir(scratch_dir):
                path = os.path.join(scratch_dir, name)
                if os.path.isfile(path):
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        os.remove(path)
                        logger.info(
                            "Scratch cleanup: removed %s (age %.1f days)",
                            path, (now - mtime) / 86400,
                        )
        except OSError as e:
            logger.error("Scratch cleanup error: %s", e)
    removed = clean_session_media()  # images/files + safe session temps (A+C)
    if removed:
        logger.info("Session temp cleanup: removed %d files", removed)
    _prune_user_locks()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def periodic_clean_scratch():
    """Run clean_scratch every 3600 seconds as a background task."""
    while True:
        try:
            await asyncio.sleep(3600)
            # 同步文件遍历放到线程池，避免阻塞事件循环卡死长轮询心跳
            await asyncio.to_thread(clean_scratch)
        except Exception as e:
            logger.exception("periodic_clean_scratch error: %s", e)


async def main_loop() -> None:
    """Main daemon loop: manages state, QR login, and message receiving."""
    ensure_runtime_dirs()
    # 同步文件遍历放到线程池，避免阻塞事件循环
    await asyncio.to_thread(clean_scratch)
    if config.update_check_enabled:
        _spawn_bg(update_check_loop())
    _spawn_bg(periodic_clean_scratch())
    while True:
        client = ILinkClient()
        # 本轮 client 的在途消息任务（强引用持有，relogin 前排空，避免拿死连接发消息）
        msg_tasks: set = set()
        try:
            state_loaded = client.state.load()

            if not state_loaded or not client.state.bot_token:
                try:
                    success = await login_flow(client)
                except Exception as e:
                    # 网络抖动/服务端异常不应直接杀死 daemon
                    logger.exception("登录流程异常，5 秒后重试: %s", e)
                    await asyncio.sleep(5)
                    continue
                if not success:
                    logger.warning("扫码超时，3 秒后重新获取二维码等待扫码")
                    await asyncio.sleep(3)
                    continue

            baseurl = client.state.baseurl
            bot_token = client.state.bot_token
            logger.info("开始长轮询 iLink 消息 (baseurl=%s)", baseurl)

            # Inner loop: long-poll for messages
            get_updates_buf = ""
            fail_delay = 0.5  # 网络异常指数退避，封顶 30s
            while True:
                try:
                    msgs, new_buf = await client.get_updates(
                        get_updates_buf, baseurl, bot_token
                    )
                    fail_delay = 0.5  # 成功一次即重置退避
                except Exception as e:
                    # Token invalidated (401/403) → break for re-login
                    logger.exception("长轮询异常: %s", e)
                    if not client.state.bot_token:
                        logger.warning("Bot token 已失效，准备重新登录")
                        break
                    # Network hiccup → 指数退避后重试
                    await asyncio.sleep(fail_delay)
                    fail_delay = min(fail_delay * 2, 30.0)
                    continue

                # Always update cursor with the server-returned value
                get_updates_buf = new_buf

                for msg in msgs:
                    msg_type = msg.get("message_type", 0)
                    if msg_type == 1:  # User message
                        if _is_duplicate_msg(msg):
                            logger.info("跳过重复投递的消息: %s", _msg_dedup_key(msg))
                            continue
                        logger.debug("inbound msg keys: %s", sorted(msg.keys()))
                        # Non-blocking async task creation: process message in background
                        t = asyncio.create_task(_safe_process_message(client, msg))
                        msg_tasks.add(t)
                        t.add_done_callback(msg_tasks.discard)
                    else:
                        logger.debug(
                            "跳过 message_type=%s", msg_type
                        )

                if not client.state.bot_token:
                    break

        except KeyboardInterrupt:
            logger.info("收到退出信号")
            raise
        finally:
            # 先排空/取消在途消息任务，再关连接，避免任务拿死 client 静默失败
            if msg_tasks:
                snapshot = set(msg_tasks)
                logger.info(
                    "等待 %d 个在途消息任务完成（最长 %.0fs）...",
                    len(snapshot), _DRAIN_TIMEOUT_S,
                )
                _, still_pending = await asyncio.wait(snapshot, timeout=_DRAIN_TIMEOUT_S)
                if still_pending:
                    logger.warning("强制取消 %d 个未完成的在途任务", len(still_pending))
                    for t in still_pending:
                        t.cancel()
                    await asyncio.gather(*still_pending, return_exceptions=True)
            await client.close()

        # Decide whether to re-login or exit
        if not client.state.bot_token:
            logger.info("Bot token 已失效，重新执行登录流程...")
            # Small delay before re-login to avoid tight loop
            await asyncio.sleep(2)
            continue  # outer loop → re-login
        else:
            # Normal exit (should not happen in steady-state)
            break


# ---------------------------------------------------------------------------
# Per-user async execution lock, global concurrency gate, background spawner
# ---------------------------------------------------------------------------
user_locks: dict = {}
_global_task_sem: asyncio.Semaphore | None = None


def _get_global_sem() -> asyncio.Semaphore:
    global _global_task_sem
    if _global_task_sem is None:
        n = max(int(config.max_concurrent_tasks), 1)
        _global_task_sem = asyncio.Semaphore(n)
    return _global_task_sem


def _prune_user_locks() -> None:
    """Drop idle per-user locks so the dict does not grow forever."""
    idle = [uid for uid, lock in user_locks.items() if not lock.locked()]
    for uid in idle:
        user_locks.pop(uid, None)


async def _safe_process_message(client: ILinkClient, msg: dict) -> None:
    """Run process_message inside global concurrency + per-user locks.

    This ensures the main get_updates long-polling loop is NEVER blocked,
    keeping WeChat heartbeats 100% active while ensuring per-user message ordering.
    """
    from_user = msg.get("from_user_id", "")
    context_token = msg.get("context_token", "")
    sem = _get_global_sem()

    # Fail fast when all slots are busy (do not queue unbounded work)
    try:
        await asyncio.wait_for(sem.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        logger.warning("并发已满，拒绝处理 from=%s", from_user)
        if context_token and from_user:
            try:
                await client.send_message(
                    to_user_id=from_user,
                    text="⏳ **系统繁忙** ⏳\n\n当前处理人数已满，请稍后再试。",
                    context_token=context_token,
                    baseurl=client.state.baseurl,
                    bot_token=client.state.bot_token,
                )
            except Exception as e:
                logger.warning("发送繁忙提示失败: %s", e)
        return

    try:
        if from_user not in user_locks:
            user_locks[from_user] = asyncio.Lock()
        async with user_locks[from_user]:
            try:
                await process_message(client, msg)
            except Exception as e:
                logger.exception("处理消息异常 (from=%s): %s", from_user, e)
    finally:
        sem.release()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(prog="wechatbridge", description="Bridge WeChat messages to agy or Grok Build CLIs — text/image/file/voice in, CLI replies and generated files back.")
    parser.add_argument("--version", action="version", version=f"wechatbridge {__version__}")
    parser.parse_args()
    logger.info("wechatbridge v%s 启动 (backend=%s, instance=%s)", __version__, config.backend, config.instance)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("进程退出")
    except Exception as e:
        logger.exception("未预期错误: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
