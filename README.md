# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge connects a WeChat bot to agentic coding CLIs (Google's agy / Antigravity, or xAI's Grok Build). From WeChat you can send text, images, files, and voice-as-text to the active CLI, get replies back, and receive certain generated files over the WeChat CDN. Switch backends per user with `/backend` — no restart.

```
WeChat (phone)  ⇄  iLink bot API  ⇄  WeChatBridge  ⇄  agy / grok CLI
                                     (this project)    (runs tools)
```

The bridge process stays up and long-polls iLink. For prompts that go to a CLI, it spawns one `agy` or `grok` child (`-p` single-turn) and exits that child when done — the child does not stay resident. Many slash commands (`/help`, `/backend`, `/persona`, …) are handled inside the bridge and never start a CLI. Only artifacts the bridge can detect under the user's allowed session paths are pushed back via CDN.

## Features

- Text, image, file, and voice (WeChat server-side transcription only) go to the **active** backend (`agy` or `grok`)
- Detected CLI artifacts under the per-user allowed tree can be sent back (size-capped); not every file the CLI touches
- Each WeChat user gets an isolated workspace; model / effort / mode are remembered **per backend**
- Runtime backend switch: `/backend agy` or `/backend grok` (clears the “continue session” flag so the next CLI turn starts without `-c` / `--continue`; does not immediately wipe history files on disk)
- Slash commands for model, session reset, persona, and more (see below)
- Dangerous-prompt gate: a **keyword list** of concrete destructive patterns asks for confirmation before run
- Sender whitelist (`WECHATBRIDGE_ALLOWED_SENDERS`; empty = allow all)
- `/mcp` returns short usage text; `/agent` rewrites into a natural-language subagent prompt for the CLI (not a native MCP bridge)
- Media over WeChat CDN with AES-128-ECB encrypt/decrypt
- Multi-instance: one codebase, set `WECHATBRIDGE_INSTANCE` per process (state / session / QR paths derive from it)
- Deploy templates: systemd (Linux), launchd (macOS), Task Scheduler notes (Windows)

## Platform Support

- **Linux** — primary (systemd unit included)
- **macOS** — supported (launchd plist included)
- **Windows** — supported (Task Scheduler guide included)

Default data paths expand from `~` (e.g. `~/.local/share/wechatbridge/<instance>/`).

## CLI Backends

- **agy** (default) — Google Antigravity CLI
- **grok** — xAI Grok Build CLI

Per-user switch: `/backend agy` or `/backend grok`. Each backend keeps its own model / effort / mode memory and persona file layout. Global default is `WECHATBRIDGE_BACKEND`.

## Prerequisites

- At least one CLI installed and signed in:
  - **agy** on `PATH`, or set `AGY_BIN_PATH`
  - **and/or grok** on `PATH`, or set `GROK_BIN_PATH`
  - Antigravity is Google's terminal agentic coding CLI (successor to Gemini CLI). Grok Build is xAI's counterpart.
- A WeChat account with a [ClawBot / iLink](https://ilinkai.weixin.qq.com) bot (QR bind on first run)
- Python 3.10+

## Install

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -r requirements.txt
```

Or install as a package:

```bash
pip install -e .
```

## Configure

```bash
cp deploy/wechatbridge.env.example .env
```

Key variables (all have defaults):

| Variable | Default | Purpose |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | path to the agy binary |
| `GROK_BIN_PATH` | `grok` | path to the grok binary |
| `WECHATBRIDGE_BACKEND` | `agy` | global default backend (`agy` / `grok`; overridable per user via `/backend`) |
| `WECHATBRIDGE_INSTANCE` | `default` | instance name; state / session / QR paths derive from it |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _empty_ | comma-separated WeChat IDs (empty = allow all) |
| `AGY_TIMEOUT` | `600` | CLI run timeout in seconds (both backends) |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | max file size sent back to WeChat (100 MB) |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | max inbound image/file after download (20 MB) |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | global concurrent handlers; extras get a busy reply |
| `WECHATBRIDGE_CONFIRM_TOKEN` | `y` | reply this token to approve a gated dangerous prompt |
| `WECHATBRIDGE_ENABLE_MCP` | `true` | enable the `/mcp` help text command |
| `WECHATBRIDGE_ENABLE_SUBAGENT` | `true` | enable the `/agent` prompt-rewrite command |

Full list: [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example).

## Run

```bash
python -m wechatbridge
```

On first run the bridge prints a QR code (and saves PNG under the instance data dir). Scan with WeChat to bind, then it long-polls for messages.

## Deploy

### Linux (systemd)

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
# edit WorkingDirectory and User
sudo systemctl enable --now wechatbridge
```

**Multi-instance:** set a different `WECHATBRIDGE_INSTANCE` in each unit. Paths for state, sessions, and QR code are derived automatically.

### macOS (launchd)

```bash
cp deploy/wechatbridge.plist ~/Library/LaunchAgents/com.wechatbridge.plist
# edit WorkingDirectory and ProgramArguments in the plist
launchctl load ~/Library/LaunchAgents/com.wechatbridge.plist
```

### Windows (Task Scheduler)

See [`deploy/wechatbridge-windows.md`](deploy/wechatbridge-windows.md).

## Slash commands

| Command | Action |
|---|---|
| `/help` | list supported commands for the active backend |
| `/backend <agy\|grok>` | switch CLI backend for this WeChat user (on real change: drop continue flag; history files may remain until retention cleanup) |
| `/clear` or `/new` | drop continue flag so the next CLI turn is a new conversation (does not instantly delete history files) |
| `/model <name>` | set model (validated against the backend's model list; see `/models`) |
| `/models` | list models from the active CLI |
| `/fast` | set low reasoning effort (**on only** — not a toggle; no “off” command) |
| `/planning` | set planning mode (**on only** — not a toggle) |
| `/add-dir <path>` | **agy:** pass `--add-dir` on later runs if path is allowed. **grok:** recorded only; not passed to the CLI yet |
| `/agents` | list agents via the active CLI |
| `/persona <text>` | set persona (`show` / `clear` / `reset` subcommands) |
| `/mcp` | short MCP **usage hint** text (can disable with `WECHATBRIDGE_ENABLE_MCP`) |
| `/agent <name> <task>` | craft a “invoke subagent …” prompt and run the CLI (can disable with `WECHATBRIDGE_ENABLE_SUBAGENT`) |

Other `/…` commands are either rejected (e.g. `/exit`), reported as unsupported on WeChat (TUI-only panels), or passed through to the active CLI.

`/add-dir` only accepts paths under the user's session directory or roots listed in `WECHATBRIDGE_ADD_DIR_ROOTS`.

## Ops & security (what the bridge actually enforces)

- **Whitelist first.** Empty `WECHATBRIDGE_ALLOWED_SENDERS` means anyone who can message the bot can use it.
- **Auto-approve CLIs.** agy runs with `--dangerously-skip-permissions`; grok with `--always-approve` (unless planning mode). Treat this as trusted-user tooling, not a multi-tenant sandbox.
- **Danger gate is keyword-based**, not full intent understanding. Defaults target concrete patterns (`rm -rf /`, pipe-to-shell, `mkfs`, `format c:`, a few heavy Chinese phrases, …). Everyday wording like bare “delete” is **not** gated. Override list via `WECHATBRIDGE_CONFIRM_KEYWORDS`; approve with `WECHATBRIDGE_CONFIRM_TOKEN` (default `y`), TTL `WECHATBRIDGE_PENDING_TTL`.
- **Inbound media** is size-capped (default 20 MB), streamed, and CDN hosts are allowlisted. Missing `aes_key` returns a clear error.
- **Outbound artifacts** only leave the allowed per-user tree (agy: session scratch; grok: under session dir), after `realpath` checks, and only if under `WECHATBRIDGE_MAX_OUTBOUND_BYTES`.
- **Concurrency:** global cap (`WECHATBRIDGE_MAX_CONCURRENT`, default 4); same user is serialized, different users can run in parallel.
- **Long replies** are split into chunks (`WECHATBRIDGE_MESSAGE_CHUNK`, default 2000 characters).
- **Data layout:** instance data under `~/.local/share/wechatbridge/<instance>/` (override with env). Runtime dirs prefer `0700`; token/QR files prefer `0600` (Unix; Windows relies on NTFS ACLs).
- **Retention:** session temps vs dialogue history use separate TTLs (`WECHATBRIDGE_SESSION_RETENTION_DAYS`, `WECHATBRIDGE_HISTORY_RETENTION_DAYS`). Prefs/auth are kept.
- **Child env** is sanitized (strips common secret-style variable names) and points `HOME` (and `USERPROFILE` on Windows) at the per-user session dir.

## Limitations

- Not a standalone agent — requires agy and/or grok.
- Voice is WeChat speech-to-text only; no local ASR; empty transcript → “type instead”.
- No video send/receive; no native WeChat voice-bubble replies (no silk encode).
- One WeChat binding per process; multiple accounts need multiple instances (`WECHATBRIDGE_INSTANCE`).
- Artifact send-back is best-effort detection under allowed paths, not “every file the CLI created anywhere”.
- `/mcp` / `/agent` do not implement MCP protocol or spawn process supervisors inside the bridge — they only guide or rephrase for the CLI.
- Deploy only for trusted users behind a whitelist when possible.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Semantic Versioning from 1.0.0; record changes in [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT. See [`LICENSE`](LICENSE).
