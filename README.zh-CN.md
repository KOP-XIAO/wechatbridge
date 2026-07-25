# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge 把微信机器人接到 agentic 编程 CLI（谷歌 agy / Antigravity，或 xAI Grok Build）。你在微信里就能读文件、跑命令、抓网页，CLI 生成的文件也会发回微信。两条后端随时用 `/backend` 切换。

```
微信(手机)  ⇄  iLink 机器人 API  ⇄  WeChatBridge  ⇄  agy / grok CLI
                                 (本项目)           (跑工具)
```

收到微信消息后，给当前用户起一个 agy 或 grok 子进程跑你的请求，跑完把结果发回微信，生成的文件走 CDN 回传。子进程是按需起的（`-p` 单轮模式），跑完就退，不常驻、不占资源。

## 功能

- 文本、图片、文件、语音消息都能从微信发给后端 CLI
- CLI 生成的文档、图片、代码会发回微信
- 每个微信用户有独立的工作区和会话
- **运行时切换后端**：`/backend agy` 或 `/backend grok`，不用重启
- slash 指令：`/model`、`/clear`、`/fast`、`/persona`、`/backend` 等
- 危险操作（删除、格式化、`rm -rf`）执行前先确认
- 白名单限定指定微信 ID 才能用
- `/mcp`、`/agent` 引导后端的 MCP 工具和子代理
- 媒体走微信 CDN，AES 加密传输
- 附带 systemd 服务文件，挂了自动拉起
- 多实例：一套代码、一份配置模板，开几个微信号就跑几个实例

## 平台支持

- **Linux** — 主要平台，完整支持（附带 systemd 服务文件）
- **macOS** — 开箱即用
- **Windows** — 开箱即用

所有默认路径基于 `~` 展开，三个平台都能正确解析。

## CLI 后端

WeChatBridge 支持多个 agentic CLI 后端，每个用户可以在运行时用 `/backend` 切换：

- **agy**（默认）— 谷歌 Antigravity CLI
- **grok** — xAI Grok Build CLI

在微信里发 `/backend agy` 或 `/backend grok` 即可切换。每个后端有自己独立的会话、人格文档和模型偏好，互不干扰。

## 前置条件

- **agy**（谷歌 Antigravity CLI）或 **grok**（xAI Grok Build CLI），至少装一个并登录好。
  - `agy` 在 `PATH` 里，或者设 `AGY_BIN_PATH`。
  - `grok` 在 `PATH` 里，或者设 `GROK_BIN_PATH`。
  - Antigravity CLI 是谷歌的终端 agentic 编程工具，能理解代码库、经授权编辑文件、在终端跑命令，是 Gemini CLI 的官方继任者。Grok Build 同类，是 xAI 的产品。
- 一个微信账号，配合 [ClawBot / iLink](https://ilinkai.weixin.qq.com) 机器人，扫码绑定。
- Python 3.10+。

## 安装

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -r requirements.txt
```

或装成本地包：

```bash
pip install -e .
```

## 配置

复制示例环境变量文件并按需改：

```bash
cp deploy/wechatbridge.env.example .env
```

关键变量（都有默认值）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | agy 可执行文件路径 |
| `GROK_BIN_PATH` | `grok` | grok 可执行文件路径 |
| `WECHATBRIDGE_BACKEND` | `agy` | 全局默认后端（`agy` 或 `grok`，可被 `/backend` 覆盖） |
| `WECHATBRIDGE_INSTANCE` | `default` | 实例名，多实例部署时区分（所有路径从它派生） |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _空_ | 允许使用的微信 ID，逗号分隔（空 = 全开） |
| `AGY_TIMEOUT` | `3600` | CLI 执行超时秒数（默认 60 分钟） |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | 回传微信的文件大小上限（100 MB） |

完整列表见 [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example)。

## 运行

```bash
python -m wechatbridge
```

首次运行会打印二维码，用微信扫码绑定机器人，之后开始收消息。

## 部署

### Linux（systemd）

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
# 编辑 WorkingDirectory 和 User
sudo systemctl enable --now wechatbridge
```

**多实例**：每个实例的 unit 里设不同的 `WECHATBRIDGE_INSTANCE`。所有路径（state、session、二维码）会自动从实例名派生，不用单独配。两个实例的 unit 除了这一行，完全一样。

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
| `/help` | 列出支持的指令 |
| `/backend <agy\|grok>` | 切换后端（按用户） |
| `/clear` 或 `/new` | 重置会话 |
| `/model <名称>` | 切换模型（用 `/models` 查列表） |
| `/models` | 列出可用模型 |
| `/fast` | 切换快速模式（低推理开销） |
| `/planning` | 切换 planning 模式 |
| `/add-dir <路径>` | 添加工作目录 |
| `/agents` | 列出可用 agent |
| `/persona <内容>` | 设置人格文档（支持 `show` / `clear` / `reset`） |
| `/mcp` | MCP 工具使用引导 |
| `/agent <名称> <任务>` | 调用子代理执行任务 |

其他 `/` 指令会直接透传给当前后端（agy 或 grok）。

## 已知限制

- 依赖 agy 或 grok，本身不是独立 agent。
- 语音准确率取决于微信的语音转文字，没有本地 ASR。
- 不收发视频——agy/grok 原生不支持理解视频，需要第三方工具，超出范围。
- 不输出原生语音气泡（没做 silk 编码）。
- 一个进程绑一个微信号，多个微信号就开多个实例（用 `WECHATBRIDGE_INSTANCE` 区分）。
- 后端以自动批准模式跑（agy 是 `--dangerously-skip-permissions`，grok 是 `--always-approve`）。请用白名单限制访问，只给可信用户用。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。项目从 1.0.0 起遵循语义化版本，每次改动登记到 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
