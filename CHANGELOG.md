# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.1] - 2026-07-25

### Fixed

- **Backend switch kept the old model**: switching `/backend` only changed the backend flag; the previous backend's model (e.g. gemini) was still passed to the new CLI and failed on first message. Preferences now remember **model / effort / mode per backend**; first visit uses empty (CLI default), later visits restore the user's last choice.
- **Grok false "Not signed in"**: per-session `auth.json` was a one-shot copy of host credentials and went stale or missing while host login remained valid. Session auth now **symlinks** to host `~/.grok/auth.json` (copy fallback).
- **Error reply style**: CLI English errors were stuffed into `❌ **...** ❌` titles. Replies now use a fixed Chinese header line plus body; known cases (login, rate limit, network, timeout, bad model, etc.) get Chinese titles.
- Failed Grok/agy runs no longer mark the session as initialized (avoids `--continue` on a broken first turn).
- agy non-zero exits with English stdout are formatted as errors instead of sent as normal replies.

### Changed

- Shared helpers in `runner_common.py`: `by_backend` prefs, `switch_backend_prefs` / `update_active_prefs`, `format_error` / `format_cli_error`, `EMPTY_REPLY`.
- `/backend` status and switch replies show the active model label.
- Version bumped to 1.2.1.

## [1.2.0] - 2026-07-25

### Added

- **Grok Build backend**: support for xAI Grok Build CLI as an alternative to agy, switchable per-user at runtime via `/backend agy|grok` command.
- **Runtime backend switching**: `/backend` slash command to switch CLI backend without restarting; each backend has isolated sessions, persona, and model preferences.
- **Multi-instance support**: `WECHATBRIDGE_INSTANCE` env var; all per-instance paths (state/session/qrcode) derive from the instance name. Deploy N instances with identical service templates.
- **Instance-derived paths**: state/session/qrcode files now live under `~/.local/share/wechatbridge/<instance>/` by default.
- Grok artifact extraction via structured `chat_history.jsonl` tool_calls (write/edit file_path).
- Persona injection for grok via `--rules` flag.
- `GROK_BIN_PATH` and `WECHATBRIDGE_BACKEND` config options.

### Changed

- Refactored shared logic (session isolation, prefs, process management, dangerous detection) into `runner_common.py` module.
- `agy.py` and new `grok.py` both import from `runner_common.py`; behavior unchanged for agy users.
- `main.py` now dispatches to active backend based on per-user preference.
- Startup log shows active backend and instance name.


### Added

- `deploy/wechatbridge.plist` — macOS launchd service template.
- `deploy/wechatbridge-windows.md` — Windows deployment guide.
- Platform Support section in both `README.md` and `README.zh-CN.md`.

## [1.0.6] - 2026-07-23

### Changed

- Replaced forced `SIGKILL` process termination with a graceful `SIGTERM` 2-second grace period to allow `agy` CLI to gracefully unlock SQLite WAL database files and close Cascade session handles, preventing Cascade lock deadlocks on subsequent `-c` invocations.
- Added `PAGER=cat`, `CI=true`, `NONINTERACTIVE=1`, and `PYTHONUNBUFFERED=1` environment flags to prevent subshell commands from hanging on headless standard input reads.


### Changed

- Increased default `AGY_TIMEOUT` from 900s to 3600s (60 minutes / 1 hour) to fully support long-horizon complex programming tasks without early process termination.
- Added automatic single-attempt retry and friendly fallback formatting when encountering `timeout waiting for cascade/response` API errors from the AI engine.


### Changed

- Refactored message polling loop to spawn non-blocking background async tasks per message (`asyncio.create_task`), ensuring the `get_updates` heartbeat channel remains 100% active 24x7 without disconnecting during long AI task executions.
- Added per-user async locks (`user_locks`) to maintain message sequence ordering per user while enabling full inter-user concurrency.


### Added

- Robust exponential backoff with random jitter retry strategy for `send_message` and `send_media_message` (up to 5 attempts covering 30-60s network recovery window).
- Failure classification: retries transient network errors & 5xx server errors, fails fast on 401/403 auth errors and 4xx client errors.


### Added

- Support for loading configuration settings from `.env` file automatically on startup.
- `.env` and `.env.example` configuration file templates for project settings.

### Changed

- Increased default `AGY_TIMEOUT` from 180 seconds (3 minutes) to 900 seconds (15 minutes) for long-running AI tasks.


### Changed

- Simplified user-facing prompt messages: removed verbose explanatory suffixes, changed "用法" to "缺少参数" where appropriate, removed parenthetical notes and redundant explanations.

## [1.0.0] - 2026-07-23

First public release.

### Added

- **Text bridge** — send text from WeChat to agy CLI, receive reply.
- **Image recognition** — send images from WeChat, bridge passes to agy for description/analysis.
- **File input** — send any file (PDF, docx, code, etc.) from WeChat; bridge decrypts and feeds to agy via path reference.
- **Voice passthrough** — WeChat voice message transcriptions (`voice_item.text`) fed to agy as text; returns a "can't hear you, please type" prompt when no transcription is available.
- **Artifact return** — files generated by agy (documents, images, code) are sent back to WeChat via CDN upload as image_item or file_item.
- **Slash commands** — `/clear`, `/model`, `/effort`, `/mode`, `/fast`, `/models`, `/mcp`, `/agent`, `/persona` for runtime control.
- **Dangerous prompt confirmation gate** — suspicious prompts (delete, format, rm -rf) intercepted with a yes/no confirmation before execution.
- **Sender whitelist** — restrict access to specific WeChat IDs (empty = allow all).
- **Per-user sessions** — isolated agy workspaces per WeChat user.
- **Scratch TTL cleanup** — periodic removal of generated artifacts older than 7 days.
- **Systemd deployment** — service files for production use with auto-restart.
