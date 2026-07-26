# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge 把微信机器人接到 agentic 编程 CLI（谷歌 agy / Antigravity，或 xAI Grok Build）。你可以在微信里发文字、图片、文件、语音（仅微信侧转写文字）给**当前后端**，拿回复，并在条件满足时把部分生成文件经 CDN 发回微信。每个用户可用 `/backend` 切换后端，无需重启进程。

```
微信(手机)  ⇄  iLink 机器人 API  ⇄  WeChatBridge  ⇄  agy / grok CLI
                                 (本项目)           (跑工具)
```

桥接进程本身一直在跑、长轮询 iLink。需要交给 CLI 的提示才会按需拉起一个 agy 或 grok 子进程（`-p` 单轮），**子进程**跑完即退、不常驻。许多 slash（如 `/help`、`/backend`、`/persona`）在桥内直接处理，不会起 CLI。只有桥能识别、且落在用户允许目录内的产物，才会经 CDN 回传。

## 功能

- 文本、图片、文件、语音（仅微信服务端语音转文字）交给**当前激活**的后端（`agy` 或 `grok`）
- 在用户允许目录内、且被识别到的 CLI 产物可回传微信（有大小上限）；不是 CLI 碰过的每个文件都会发
- 每个微信用户独立工作区；模型 / 推理强度 / 模式按**后端**分别记忆
- 运行时切换后端：`/backend agy` 或 `/backend grok`（真正切换时清掉「续聊」标记，下次 CLI 不再带 `-c` / `--continue`；磁盘上的历史文件不会立刻抹掉）
- slash 指令：模型、清会话、人格等（见下表）
- 危险提示闸门：对**明确破坏性关键词/模式**先确认再跑（不是全语义理解）
- 白名单 `WECHATBRIDGE_ALLOWED_SENDERS`（空 = 全开）
- `/mcp` 只回使用说明；`/agent` 改写成自然语言子代理提示再交给 CLI（**不是**桥内原生 MCP 协议）
- 媒体走微信 CDN，AES-128-ECB 加解密
- 多实例：一套代码，用 `WECHATBRIDGE_INSTANCE` 区分进程（state / 会话 / 二维码路径由实例名派生）
- 部署模板：Linux systemd、macOS launchd、Windows 任务计划说明

## 平台支持

- **Linux** — 主力（附 systemd）
- **macOS** — 支持（附 launchd plist）
- **Windows** — 支持（附任务计划说明）

默认数据路径从 `~` 展开（如 `~/.local/share/wechatbridge/<instance>/`）。

## CLI 后端

- **agy**（默认）— 谷歌 Antigravity CLI
- **grok** — xAI Grok Build CLI

微信里 `/backend agy` 或 `/backend grok` 按用户切换。各后端各自记模型 / 强度 / 模式，人格文件布局也分开。全局默认见 `WECHATBRIDGE_BACKEND`。

## 前置条件

- 至少装好并登录其中一个 CLI：
  - **agy** 在 `PATH` 中，或设 `AGY_BIN_PATH`
  - **和/或 grok** 在 `PATH` 中，或设 `GROK_BIN_PATH`
  - Antigravity 是谷歌终端 agentic 编程工具（Gemini CLI 官方继任）；Grok Build 是 xAI 同类产品
- 一个微信账号 + [ClawBot / iLink](https://ilinkai.weixin.qq.com) 机器人（首次扫码绑定）
- Python 3.10+

## 安装

推荐使用 [pipx](https://pypa.github.io/pipx/)（需要 Python >= 3.10）：

```bash
pipx install wechatbridge
```

安装后验证：

```bash
wechatbridge --version
```

### 安装 pipx

**Debian / Ubuntu：**

```bash
sudo apt install pipx
```

**其他系统（或想装最新版）：**

```bash
python3 -m pip install --user pipx && python3 -m pipx ensurepath
```

然后重新打开终端或重新加载 shell 配置文件，确保 `pipx` 在 `PATH` 中。

### 开发者

如果你想从源码修改：

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -e .
```

## 配置

配置加载优先级从高到低：

1. `$WECHATBRIDGE_ENV_FILE` — 显式指定路径
2. `$XDG_CONFIG_HOME/wechatbridge/<实例名>.env`（缺省 `~/.config/wechatbridge/<实例名>.env`）
3. `$XDG_CONFIG_HOME/wechatbridge/.env`（缺省 `~/.config/wechatbridge/.env`）
4. 仓库根目录 `.env` — **已废弃**（启动时会打印警告）

实例名缺省为 `default`；可通过 `WECHATBRIDGE_INSTANCE` 修改。

获取示例配置：

```bash
mkdir -p ~/.config/wechatbridge
curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
```

然后编辑 `~/.config/wechatbridge/.env` 修改你的配置。

关键变量（都有默认值）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | agy 可执行文件路径 |
| `GROK_BIN_PATH` | `grok` | grok 可执行文件路径 |
| `WECHATBRIDGE_BACKEND` | `agy` | 全局默认后端（`agy` / `grok`，可被 `/backend` 按用户覆盖） |
| `WECHATBRIDGE_INSTANCE` | `default` | 实例名；state / 会话 / 二维码路径由它派生 |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _空_ | 允许使用的微信 ID，逗号分隔（空 = 全开） |
| `AGY_TIMEOUT` | `600` | CLI 执行超时秒数（两个后端共用） |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | 回传微信文件大小上限（100 MB） |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | 入站图片/文件下载后上限（20 MB） |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | 全局并发处理数；超出回「忙」 |
| `WECHATBRIDGE_CONFIRM_TOKEN` | `y` | 危险闸门确认口令 |
| `WECHATBRIDGE_ENABLE_MCP` | `true` | 是否启用 `/mcp` 说明指令 |
| `WECHATBRIDGE_ENABLE_SUBAGENT` | `true` | 是否启用 `/agent` 提示改写指令 |
| `WECHATBRIDGE_ADMINS` | _空_ | 逗号分隔的 wxid 列表；检测到新版本时管理员会收到微信通知 |
| `WECHATBRIDGE_UPDATE_CHECK` | `true` | 启动时及每 24h 检查 PyPI 新版本；失败静默不影响运行 |
| `WECHATBRIDGE_UPDATE_CHECK_INTERVAL` | `86400` | 版本检查间隔（秒） |

完整列表见 [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example)。

> **为什么改配置位置？** pipx 全局安装后，源码目录下的 `.env` 不再合理。XDG 基础目录布局将配置与代码分离，并且天然支持多实例。

## 运行

```bash
wechatbridge
```

首次运行会打印二维码（并在实例数据目录保存 PNG）。微信扫码绑定后开始收消息。

## 升级

```bash
pipx upgrade wechatbridge
sudo systemctl restart wechatbridge
```

或运行升级脚本（无需 clone 仓库，直接用 curl 获取）：

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

脚本会自动升级 pipx 安装并重启服务。如果服务运行在专用系统用户下（如 `wechatbridge`），以 root 运行时会自动以该用户身份执行 pipx（可用 `WECHATBRIDGE_USER=<用户名>` 覆盖）。

数据存放在 `~/.local/share/wechatbridge/<实例名>/`（会话、SQLite 历史、二维码、登录态），升级**不会**影响——你的 bot 保持登录，对话不丢失。

升级 **major** 或 **minor** 版本（例如 1.2 → 1.3）前，请先查阅 [`CHANGELOG.md`](CHANGELOG.md) 中对应版本的破坏性变更和迁移步骤。

## 部署

### Linux（systemd）

首先，在 `wechatbridge` 系统用户下安装：

```bash
sudo -u wechatbridge pipx install wechatbridge
```

然后部署服务 unit：

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge
```

**多实例：** 复制模板 `deploy/wechatbridge@.service` 并启动实例：

```bash
sudo cp deploy/wechatbridge@.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge@bot2
sudo systemctl enable --now wechatbridge@bot3
```

每个实例读取自己的配置文件（`~/.config/wechatbridge/bot2.env`），数据存放在各自的数据目录（`~/.local/share/wechatbridge/bot2/`）。

### macOS（launchd）

```bash
cp deploy/wechatbridge.plist ~/Library/LaunchAgents/com.wechatbridge.plist
# 编辑 plist 里的 WorkingDirectory 和 ProgramArguments
launchctl load ~/Library/LaunchAgents/com.wechatbridge.plist
```

### Windows（任务计划程序）

见 [`deploy/wechatbridge-windows.md`](deploy/wechatbridge-windows.md)。

## slash 指令

| 指令 | 作用 |
|---|---|
| `/help` | 按当前后端列出支持指令 |
| `/backend <agy\|grok>` | 按微信用户切换 CLI 后端（真切换时丢掉续聊标记；历史文件可能仍在，靠保留策略清理） |
| `/clear` 或 `/new` | 丢掉续聊标记，下次 CLI 起新对话（不会立刻删除历史文件） |
| `/model <名称>` | 设模型（对照该后端模型列表校验；见 `/models`） |
| `/models` | 列出当前 CLI 可用模型 |
| `/fast` | 设为低推理开销（**只开不关**，不是来回切换） |
| `/planning` | 设为 planning 模式（**只开不关**） |
| `/add-dir <路径>` | **agy：** 校验通过后后续会带 `--add-dir`。**grok：** 只记偏好，暂不传给 CLI |
| `/agents` | 通过当前 CLI 列 agent |
| `/persona <内容>` | 设人格（`show` / `clear` / `reset`） |
| `/version` | 显示当前版本、实例名和后端；若有新版本则显示升级提示 |
| `/mcp` | 短 **使用说明** 文案（可用 `WECHATBRIDGE_ENABLE_MCP` 关掉） |
| `/agent <名称> <任务>` | 拼成「调用子代理…」提示再跑 CLI（可用 `WECHATBRIDGE_ENABLE_SUBAGENT` 关掉） |

其余 `/…`：有的在微信端禁用（如 `/exit`），有的是 TUI 专用会提示不支持，其余透传给当前 CLI。

`/add-dir` 只接受用户会话目录内，或 `WECHATBRIDGE_ADD_DIR_ROOTS` 列出的根路径下的目录。

## 运维与安全（桥实际管到的）

- **白名单优先。** `WECHATBRIDGE_ALLOWED_SENDERS` 为空 = 能私聊机器人的人都能用。
- **CLI 自动批准。** agy 带 `--dangerously-skip-permissions`；grok 带 `--always-approve`（planning 模式除外）。只适合可信用户，不是多租户沙箱。
- **危险闸门是关键词匹配**，不是完整意图识别。默认针对具体模式（如 `rm -rf /`、管道进 shell、`mkfs`、`format c:`、少量重型中文句式等）。日常里单独一个「删除」**不会**拦。可用 `WECHATBRIDGE_CONFIRM_KEYWORDS` 自定义；确认口令 `WECHATBRIDGE_CONFIRM_TOKEN`（默认 `y`），等待 `WECHATBRIDGE_PENDING_TTL`。
- **入站媒体**有大小上限（默认 20 MB）、流式下载、CDN 域名白名单；缺 `aes_key` 会明确报错。
- **出站产物**只从用户允许目录发出（agy：会话 scratch；grok：会话目录下），经 `realpath` 检查，且不超过 `WECHATBRIDGE_MAX_OUTBOUND_BYTES`。
- **并发：** 全局上限默认 4；同一用户串行，不同用户可并行。
- **长回复**按字数切块（`WECHATBRIDGE_MESSAGE_CHUNK`，默认 2000）。
- **数据目录：** 默认 `~/.local/share/wechatbridge/<instance>/`（可 env 覆盖）。运行目录倾向 `0700`，token/二维码倾向 `0600`（Unix；Windows 依赖 NTFS ACL）。
- **清理：** 会话临时文件与对话历史用不同 TTL（`WECHATBRIDGE_SESSION_RETENTION_DAYS`、`WECHATBRIDGE_HISTORY_RETENTION_DAYS`）。偏好/登录信息不按此删。
- **子进程环境**会剥常见密钥类变量名，并把 `HOME`（Windows 另设 `USERPROFILE`）指到该用户会话目录。

## 已知限制

- 依赖 agy 和/或 grok，本身不是独立 agent。
- 语音只靠微信转写；无本地 ASR；转写为空会提示改打字。
- 不收发视频；不回原生语音气泡（未做 silk 编码）。
- 一个进程绑一个微信号；多号多实例（`WECHATBRIDGE_INSTANCE`）。
- 产物回传是「允许路径内、能识别到的尽量发」，不是「CLI 在任意位置写的都回传」。
- `/mcp`、`/agent` 不在桥内实现 MCP 协议或托管子进程，只引导或改写提示给 CLI。
- 尽量加白名单，只给可信用户用。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。从 1.0.0 起语义化版本，改动记入 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
