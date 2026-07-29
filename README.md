# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge connects a WeChat bot to agentic coding CLIs (Google's agy / Antigravity, xAI's Grok Build, or OpenAI's Codex). From WeChat you can send text, images, files, and voice-as-text to the active CLI, get replies back, and receive certain generated files over the WeChat CDN. Switch backends per user with `/backend` — no restart.

```
WeChat (phone)  ⇄  iLink bot API  ⇄  WeChatBridge  ⇄  agy / grok / codex CLI
                                     (this project)    (runs tools)
```

The bridge process stays up and long-polls iLink. For prompts that go to a CLI, it spawns one `agy` or `grok` child (`-p` single-turn) and exits that child when done — the child does not stay resident. Many slash commands (`/help`, `/backend`, `/persona`, …) are handled inside the bridge and never start a CLI. Only artifacts the bridge can detect under the user's allowed session paths are pushed back via CDN.

## Features

- Text, image, file, and voice (WeChat server-side transcription only) go to the **active** backend (`agy`, `grok`, or `codex`)
- Detected CLI artifacts under the per-user allowed tree can be sent back (size-capped); not every file the CLI touches
- Each WeChat user gets an isolated workspace; model / effort / mode are remembered **per backend**
- Runtime backend switch: `/backend agy`, `/backend grok`, or `/backend codex` (clears that backend's continuation state — the agy/grok continuation flag and the codex `thread_id`/resume state — so the next CLI turn starts a fresh session; history files on disk are not wiped immediately)
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
- **codex** — OpenAI Codex CLI

Per-user switch: `/backend agy`, `/backend grok`, or `/backend codex`. Each backend keeps its own model / effort / mode memory and persona file layout. Global default is `WECHATBRIDGE_BACKEND`.

### Codex backend notes

- Runs `codex exec --json` for each single turn; conversation continuation uses `codex exec resume <thread_id> <prompt>` with the thread id persisted per user.
- Isolation: each WeChat user runs with `HOME` and `CODEX_HOME` pointed at their own per-user session directory (`session_dir/.codex`), so sessions, logs, and caches never cross users.
- Auth: the per-user session links to the host `~/.codex/auth.json` (copied as a fallback), reusing the host `codex login`. Alternatively, set `CODEX_API_KEY` in the bridge process environment to authenticate. No key or token values are stored in this repository.
- **Status:** there is currently no real Codex subscription or CLI available for live testing. The codex backend is implemented from source research, a JSONL fixture, and a fake CLI used by the test suite (which passes). Final acceptance depends on a real user running it against the actual Codex CLI.

## Prerequisites

- At least one CLI installed and signed in:
  - **agy** on `PATH`, or set `AGY_BIN_PATH`
  - **and/or grok** on `PATH`, or set `GROK_BIN_PATH`
  - **and/or codex** on `PATH`, or set `CODEX_BIN_PATH`
  - Antigravity is Google's terminal agentic coding CLI (successor to Gemini CLI). Grok Build is xAI's counterpart; Codex is OpenAI's terminal agentic coding CLI.
- A WeChat account with a [ClawBot / iLink](https://ilinkai.weixin.qq.com) bot (QR bind on first run)
- Python 3.10+

## Install

The recommended way is with [pipx](https://pypa.github.io/pipx/) (Python >= 3.10 required):

```bash
pipx install wechatbridge-cli
```

After installation, verify:

```bash
wechatbridge --version
```

### Install pipx

**Debian / Ubuntu:**

```bash
sudo apt install pipx
```

**Other systems (or to get the latest version):**

```bash
python3 -m pip install --user pipx && python3 -m pipx ensurepath
```

Then start a new shell or re-source your shell config so `pipx` is on `PATH`.

### Developers

If you want to hack on the source:

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -e .
```

## Configure

Configuration is loaded from the first location found:

1. `$WECHATBRIDGE_ENV_FILE` — explicit path
2. `$XDG_CONFIG_HOME/wechatbridge/<instance>.env` (defaults to `~/.config/wechatbridge/<instance>.env`)
3. `$XDG_CONFIG_HOME/wechatbridge/.env` (defaults to `~/.config/wechatbridge/.env`)
4. `.env` in the repository root — **deprecated** (prints a warning on startup)

The instance name defaults to `default`; override with `WECHATBRIDGE_INSTANCE`.

Get the example config:

```bash
mkdir -p ~/.config/wechatbridge
curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
```

Then edit `~/.config/wechatbridge/.env` with your settings.

Key variables (all have defaults):

| Variable | Default | Purpose |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | path to the agy binary |
| `GROK_BIN_PATH` | `grok` | path to the grok binary |
| `CODEX_BIN_PATH` | `codex` | path to the codex binary |
| `WECHATBRIDGE_BACKEND` | `agy` | global default backend (`agy` / `grok` / `codex`; overridable per user via `/backend`) |
| `WECHATBRIDGE_INSTANCE` | `default` | instance name; state / session / QR paths derive from it |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _empty_ | comma-separated WeChat IDs (empty = allow all) |
| `AGY_TIMEOUT` | `600` | CLI run timeout in seconds (all three backends) |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | max file size sent back to WeChat (100 MB) |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | max inbound image/file after download (20 MB) |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | global concurrent process slots; same user serial (queue does not hold a slot); extras get a busy reply |
| `WECHATBRIDGE_CONFIRM_TOKEN` | `y` | reply this token to approve a gated dangerous prompt |
| `WECHATBRIDGE_ENABLE_MCP` | `true` | enable the `/mcp` help text command |
| `WECHATBRIDGE_ENABLE_SUBAGENT` | `true` | enable the `/agent` prompt-rewrite command |
| `WECHATBRIDGE_ADMINS` | _empty_ | comma-separated wxid list; admins receive WeChat notification when a new version is detected |
| `WECHATBRIDGE_UPDATE_CHECK` | `true` | check PyPI for new versions on startup and every 24h; failures are silent |
| `WECHATBRIDGE_UPDATE_CHECK_INTERVAL` | `86400` | update check interval in seconds |

Full list: [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example).

> **Why the new config location?** With pipx the package is installed globally, so a `.env` next to the source no longer makes sense. The XDG base directory layout keeps your config separate and instance-aware.

## Run

```bash
wechatbridge
```

On first run the bridge prints a QR code (and saves PNG under the instance data dir). Scan with WeChat to bind, then it long-polls for messages.

## Upgrading

```bash
pipx upgrade wechatbridge-cli
sudo systemctl restart wechatbridge
```

Or run the upgrade script (no clone needed — fetch it with curl):

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

The script upgrades the pipx installation and restarts the service. If the service runs as a dedicated system user (e.g. `wechatbridge`), running as root automatically runs pipx as that user (override with `WECHATBRIDGE_USER=<user>`).

Data lives under `~/.local/share/wechatbridge/<instance>/` (sessions, SQLite history, QR codes, login state) and is **not** touched during upgrade — your bots stay logged in and conversations are preserved.

Before upgrading a **major** or **minor** version (e.g. 1.2 → 1.3), check the corresponding section in [`CHANGELOG.md`](CHANGELOG.md) for breaking changes and migration steps.

## Deploy

### Linux (systemd)

First, install the bridge under the `wechatbridge` system user:

```bash
sudo -u wechatbridge pipx install wechatbridge-cli
```

Then deploy the service unit:

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge
```

**Multi-instance:** copy the template `deploy/wechatbridge@.service` and enable instances:

```bash
sudo cp deploy/wechatbridge@.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge@bot2
sudo systemctl enable --now wechatbridge@bot3
```

Each instance reads its own config file (`~/.config/wechatbridge/bot2.env`) and keeps state under its own data directory (`~/.local/share/wechatbridge/bot2/`).

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
| `/backend <agy\|grok\|codex>` | switch CLI backend for this WeChat user (on real change: clears that backend's continuation state — agy/grok flag and codex `thread_id`/resume — so the next turn starts a fresh session; history files may remain until retention cleanup) |
| `/clear` or `/new` | drop continue flag so the next CLI turn is a new conversation (does not instantly delete history files) |
| `/model <name>` | set model (all backends validate against a live list: agy/grok via CLI `models`; codex via `codex debug models` [then `--bundled`]; unknown name or list-fetch failure refuse and do not write prefs; see `/models`) |
| `/models` | list models — agy/grok/codex all query the live CLI (codex: `debug models`; falls back to a built-in reference note only if the live list cannot be fetched) |
| `/fast` | set low reasoning effort (**on only** — not a toggle; no “off” command) |
| `/planning` | set planning mode (**on only** — not a toggle) |
| `/add-dir <path>` | **agy:** pass `--add-dir` on later runs if path is allowed. **grok:** recorded only; not passed to the CLI yet |
| `/agents` | list agents via the active CLI |
| `/persona <text>` | set persona (`show` / `clear` / `reset` subcommands) |
| `/version` | show current version, instance name, and backend; if a newer version is available, show upgrade hint |
| `/mcp` | short MCP **usage hint** text (can disable with `WECHATBRIDGE_ENABLE_MCP`) |
| `/agent <name> <task>` | craft a "invoke subagent …" prompt and run the CLI (can disable with `WECHATBRIDGE_ENABLE_SUBAGENT`) |

Other `/…` commands are either rejected (e.g. `/exit`), reported as unsupported on WeChat (TUI-only panels), or passed through to the active CLI.

`/add-dir` only accepts paths under the user's session directory or roots listed in `WECHATBRIDGE_ADD_DIR_ROOTS`.

## Ops & security (what the bridge actually enforces)

- **Whitelist first.** Empty `WECHATBRIDGE_ALLOWED_SENDERS` means anyone who can message the bot can use it.
- **Auto-approve CLIs.** agy runs with `--dangerously-skip-permissions`; grok with `--always-approve` (unless planning mode). Treat this as trusted-user tooling, not a multi-tenant sandbox.
- **Danger gate is keyword-based**, not full intent understanding. Defaults target concrete patterns (`rm -rf /`, pipe-to-shell, `mkfs`, `format c:`, a few heavy Chinese phrases, …). Everyday wording like bare “delete” is **not** gated. Override list via `WECHATBRIDGE_CONFIRM_KEYWORDS`; approve with `WECHATBRIDGE_CONFIRM_TOKEN` (default `y`), TTL `WECHATBRIDGE_PENDING_TTL`.
- **Inbound media** is size-capped (default 20 MB), streamed, and CDN hosts are allowlisted. Missing `aes_key` returns a clear error.
- **Outbound artifacts** only leave the allowed per-user tree (agy: session scratch; grok: under session dir), after `realpath` checks, and only if under `WECHATBRIDGE_MAX_OUTBOUND_BYTES`.
- **Concurrency:** global process-slot cap (`WECHATBRIDGE_MAX_CONCURRENT`, default 4). Same user is serialized and does **not** hold a global slot while waiting on their previous message; different users can run in parallel up to the cap.
- **Long replies** are split into chunks (`WECHATBRIDGE_MESSAGE_CHUNK`, default 2000 characters).
- **Data layout:** instance data under `~/.local/share/wechatbridge/<instance>/` (override with env). Runtime dirs prefer `0700`; token/QR files prefer `0600` (Unix; Windows relies on NTFS ACLs).
- **Retention:** session temps vs dialogue history use separate TTLs (`WECHATBRIDGE_SESSION_RETENTION_DAYS`, `WECHATBRIDGE_HISTORY_RETENTION_DAYS`). Prefs/auth are kept.
- **Child env** is sanitized (strips common secret-style variable names) and points `HOME` (and `USERPROFILE` on Windows) at the per-user session dir.

## Limitations

- Not a standalone agent — requires agy and/or grok and/or codex.
- The **codex** backend is not yet verified against a real Codex subscription/CLI; it is validated by source research, a JSONL fixture, and a fake CLI in tests. Treat it as community-tested until a real user confirms.
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
