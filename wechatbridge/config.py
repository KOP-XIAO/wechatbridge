"""
wechatbridge Configuration Module
Settings with environment variable overrides.
"""

import os
import logging

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


def _load_env_file():
    """Automatically load .env file if present."""
    env_path = os.getenv("WECHATBRIDGE_ENV_FILE", os.path.join(os.path.dirname(_BASE_DIR), ".env"))
    if not os.path.exists(env_path):
        env_path = os.path.join(_BASE_DIR, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception as e:
            logger.warning("Failed to load .env file %s: %s", env_path, e)


_load_env_file()


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %d", name, val, default)
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %f", name, val, default)
        return default


# ---------------------------------------------------------------------------
# Instance identity — all per-instance paths derive from this
# ---------------------------------------------------------------------------
_instance = os.getenv("WECHATBRIDGE_INSTANCE", "default")
_instance_data_dir = os.path.join(
    os.path.expanduser("~"), ".local", "share", "wechatbridge", _instance
)


class AppConfig:
    # iLink base URL (no trailing slash)
    ilink_base_url: str = os.getenv("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")

    # Active CLI backend: "agy" or "grok" (global default, can be overridden per-user via /backend)
    backend: str = os.getenv("WECHATBRIDGE_BACKEND", "agy").lower()
    if backend not in ("agy", "grok"):
        logger.warning("Unknown backend %r, falling back to 'agy'", backend)
        backend = "agy"

    # agy CLI binary path
    agy_binary_path: str = os.getenv("AGY_BIN_PATH", "agy")  # default assumes in PATH

    # grok CLI binary path
    grok_binary_path: str = os.getenv("GROK_BIN_PATH", "grok")  # default assumes in PATH

    # Instance name (for multi-instance deployments)
    instance: str = _instance

    # Per-instance paths (derived from instance, can be overridden by explicit env vars)
    session_base_dir: str = os.getenv(
        "WECHATBRIDGE_SESSION_DIR",
        os.path.join(_instance_data_dir, "sessions"),
    )

    state_file_path: str = os.getenv(
        "WECHATBRIDGE_STATE_FILE",
        os.path.join(_instance_data_dir, ".ilink_state.json"),
    )

    qrcode_png_path: str = os.getenv(
        "WECHATBRIDGE_QRCODE_PATH",
        os.path.join(_instance_data_dir, "qrcode.png"),
    )

    qrcode_url_path: str = os.getenv(
        "WECHATBRIDGE_QRCODE_URL_FILE",
        os.path.join(_instance_data_dir, ".current_qrcode_url.txt"),
    )

    # Timeout for CLI execution (seconds) — default 600s; override via AGY_TIMEOUT
    agy_timeout: int = _env_int("AGY_TIMEOUT", 600)

    # QR code polling timeout (seconds)
    qrcode_poll_timeout: int = _env_int("QRCODE_POLL_TIMEOUT", 180)

    # QR code poll interval (seconds)
    qrcode_poll_interval: float = _env_float("QRCODE_POLL_INTERVAL", 1.5)

    # Log level
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # iLink CDN base URL for image download
    cdn_base_url: str = os.getenv("WECHATBRIDGE_CDN_BASE", "https://novac2c.cdn.weixin.qq.com/c2c")

    # agy scratch directory (where agy writes generated files)
    agy_scratch_dir: str = os.getenv("AGY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity-cli/scratch"))

    # Global agy scratch retention days (TTL cleanup)
    scratch_retention_days: int = _env_int("AGY_SCRATCH_RETENTION_DAYS", 7)

    # Per-session temp cleanup (media, .cache, scratch/logs) — not dialogue history.
    # Defaults to the same value as scratch_retention_days.
    session_retention_days: int = _env_int(
        "WECHATBRIDGE_SESSION_RETENTION_DAYS",
        _env_int("AGY_SCRATCH_RETENTION_DAYS", 7),
    )

    # Dialogue history idle TTL: conversations/brain/grok sessions untouched this
    # many days are deleted. Active chats (files still updated) are kept.
    history_retention_days: int = _env_int(
        "WECHATBRIDGE_HISTORY_RETENTION_DAYS", 30
    )

    # Maximum outbound file size (bytes) — 100 MB, Tencent OpenClaw SDK limit
    max_outbound_file_bytes: int = _env_int("WECHATBRIDGE_MAX_OUTBOUND_BYTES", 100 * 1024 * 1024)

    # Maximum inbound image/file size after download (bytes) — default 20 MB
    max_inbound_file_bytes: int = _env_int("WECHATBRIDGE_MAX_INBOUND_BYTES", 20 * 1024 * 1024)

    # Max concurrent message handlers (global). Extra messages get a busy reply.
    max_concurrent_tasks: int = _env_int("WECHATBRIDGE_MAX_CONCURRENT", 4)

    # WeChat text chunk size (characters) when splitting long replies
    message_chunk_chars: int = _env_int("WECHATBRIDGE_MESSAGE_CHUNK", 2000)

    # Extra roots allowed for /add-dir (comma-separated absolute paths).
    # Session dir is always allowed. Empty = only session dir (and its children).
    add_dir_roots: list = [
        os.path.expanduser(s.strip())
        for s in os.getenv("WECHATBRIDGE_ADD_DIR_ROOTS", "").split(",")
        if s.strip()
    ]

    # CDN upload timeout (seconds)
    cdn_upload_timeout: int = _env_int("CDN_UPLOAD_TIMEOUT", 120)

    # Access control: comma-separated wxid list, empty = allow all
    allowed_senders: list = [
        s.strip()
        for s in os.getenv("WECHATBRIDGE_ALLOWED_SENDERS", "").split(",")
        if s.strip()
    ]

    # Enable /mcp slash command (agy MCP tool guidance)
    enable_mcp: bool = os.getenv("WECHATBRIDGE_ENABLE_MCP", "true").lower() == "true"

    # Enable /agent slash command (subagent invocation)
    enable_subagent: bool = os.getenv("WECHATBRIDGE_ENABLE_SUBAGENT", "true").lower() == "true"

    # Confirm gate: dangerous prompt confirmation (empty = fallback to hardcoded list)
    confirm_keywords: list = [
        kw.strip()
        for kw in os.getenv("WECHATBRIDGE_CONFIRM_KEYWORDS", "").split(",")
        if kw.strip()
    ]
    # TTL for pending confirmations (seconds)
    pending_confirm_ttl: int = _env_int("WECHATBRIDGE_PENDING_TTL", 300)
    # Confirmation keyword users must reply to execute dangerous prompt
    confirm_token: str = os.getenv("WECHATBRIDGE_CONFIRM_TOKEN", "y")


config = AppConfig()


def ensure_runtime_dirs() -> None:
    """Create instance data / session / state / qrcode parent dirs with tight perms."""
    paths = {
        _instance_data_dir,
        config.session_base_dir,
        os.path.dirname(os.path.abspath(config.state_file_path)) or ".",
        os.path.dirname(os.path.abspath(config.qrcode_png_path)) or ".",
        os.path.dirname(os.path.abspath(config.qrcode_url_path)) or ".",
    }
    for path in paths:
        if not path or path == ".":
            continue
        try:
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o700)
        except OSError as e:
            logger.warning("Failed to ensure runtime dir %s: %s", path, e)
