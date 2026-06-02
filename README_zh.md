# Clawside

![clawside](./assets/README_img.png)

<div align="center">
  <p>
    <a href="./README.md">English</a> |
    <a href="./README_zh.md">简体中文</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/python-%E2%89%A53.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/runtime-Docker-2496ED?logo=docker&logoColor=white" alt="Docker">
    <img src="https://img.shields.io/badge/LLM-OpenAI%20compatible-412991" alt="OpenAI Compatible">
    <img src="https://img.shields.io/badge/MCP-tools-6f42c1" alt="MCP">
    <img src="https://img.shields.io/badge/channel-Telegram-26A5E4?logo=telegram&logoColor=white" alt="Telegram">
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
    <a href="https://github.com/Caldalis/clawside/graphs/commit-activity"><img src="https://img.shields.io/github/commit-activity/m/Caldalis/clawside" alt="Commits last month"></a>
    <a href="https://github.com/Caldalis/clawside/issues"><img src="https://img.shields.io/github/issues/Caldalis/clawside" alt="Issues"></a>
  </p>
</div>

Clawside 是一个 Python 编写的 agent 编排系统。它由一个长期运行的主机进程和按会话启动的 Docker agent 容器组成：主机进程负责接收 CLI 或 Telegram 消息、路由到具体 agent、写入会话队列、启动容器；容器负责运行 OpenAI 兼容的 ReAct/tool-use 循环，并通过 MCP 工具把回复、定时任务、询问用户等结果写回主机可读取的队列。

当前项目的实际能力：

- 主机端：Python 3.11+、asyncio、SQLite、Docker。
- 容器端：Python、OpenAI Chat Completions、MCP stdio。
- 渠道：本地 CLI、Telegram long polling。
- Agent：用 `agent_group` 表示，每个 agent group 对应 `groups/<folder>/` 目录。
- 运行时队列：每个 session 有自己的 `inbound.db` 和 `outbound.db`。
- 内置 MCP 工具：发消息、发文件、读写文件、运行 bash、定时任务、询问用户、加载 skill。

## 工作原理

整体链路是：

```text
用户
  -> ChannelAdapter，例如 CLI 或 Telegram
  -> src.router.route_inbound()
  -> session 的 inbound.db
  -> Docker agent 容器
  -> OpenAI Chat Completions + MCP tools
  -> session 的 outbound.db
  -> src.delivery
  -> ChannelAdapter.deliver()
  -> 用户
```

项目刻意把数据拆成三类：

- `data/v2.db`：中心数据库，存全局配置、agent、群绑定、权限、session 索引、pending question 等。
- `inbound.db`：每个 session 一个，主机写，容器读。
- `outbound.db`：每个 session 一个，容器写，主机读。

每个会话目录长这样：

```text
data/v2-sessions/
  <agent_group_id>/
    <session_id>/
      inbound.db
      outbound.db
      .heartbeat
      inbox/
      outbox/
```

关键约束：

- `inbound.db` 只由主机写入。
- `outbound.db` 只由容器写入。
- 主机通过 `processing_ack` 和 `.heartbeat` 判断容器是否正在处理、是否卡住、是否需要重试。
- host 写入 `messages_in` 使用偶数 `seq`。
- container 写入 `messages_out` 使用奇数 `seq`。

## 环境要求

- Python 3.11+
- Docker
- OpenAI 兼容 API key
- 如果接入 Telegram，需要 Telegram bot token

安装依赖：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install \
  "python-telegram-bot>=21.0" \
  "structlog>=24.0" \
  "python-dotenv>=1.0" \
  "croniter>=2.0"
```

运行命令需要在项目根目录执行。主机进程使用 `python -m src.main` 启动，不需要 editable package install。

构建 agent 镜像：

```bash
bash container/build.sh
```

默认会构建：

```text
clawside-agent:latest
```

如果你设置了 `CONTAINER_IMAGE`，则使用你设置的镜像名。

## 配置 `.env`

在项目根目录创建 `.env`：

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
DEFAULT_MODEL=gpt-4o

CONTAINER_IMAGE=clawside-agent:latest
CONTAINER_RUNTIME=docker

TIMEZONE=Asia/Shanghai
DATA_DIR=./data
GROUPS_DIR=./groups
ASSISTANT_NAME=Andy
CLI_SOCKET_PATH=data/clawside.sock

# 可选：只有接入 Telegram 才需要
TELEGRAM_BOT_TOKEN=123456789:ABC_xxx
```

主要环境变量：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `OPENAI_API_KEY` | 传给 agent 容器的 API key | 无 |
| `OPENAI_BASE_URL` | OpenAI 兼容 API 地址 | `https://api.openai.com/v1` |
| `DEFAULT_MODEL` | 默认模型 | `gpt-4o` |
| `CONTAINER_IMAGE` | agent 容器镜像 | `clawside-agent:latest` |
| `CONTAINER_RUNTIME` | 容器运行时 | `docker` |
| `TELEGRAM_BOT_TOKEN` | 存在时启用 Telegram adapter | 无 |
| `TIMEZONE` | 用户时区和定时任务时区 | `UTC` |
| `DATA_DIR` | 中心库和 session 库目录 | `./data` |
| `GROUPS_DIR` | agent group 目录 | `./groups` |
| `ASSISTANT_NAME` | 默认 agent 名称 | `Andy` |
| `CLI_SOCKET_PATH` | 管理接口 socket 路径 | `data/clawside.sock` |

运行参数：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `ACTIVE_POLL_MS` | 运行中 session 的投递轮询间隔 | `1000` |
| `SWEEP_POLL_MS` | host sweep 间隔 | `60000` |
| `ABSOLUTE_CEILING_MS` | heartbeat 最大陈旧时间，超过可 kill 容器 | `1800000` |
| `CLAIM_STUCK_MS` | processing claim 卡住判定时间 | `60000` |
| `MAX_TRIES` | 消息最大重试次数 | `5` |
| `BACKOFF_BASE_MS` | 重试退避基数 | `5000` |
| `MAX_DELIVERY_ATTEMPTS` | 出站消息最大投递次数 | `3` |

## 本地启动

启动主机进程：

```bash
python -m src.main
```

或者：

```bash
make dev
```

第一次启动会自动创建：

- `data/v2.db`
- 默认用户 `cli:local`
- 默认 agent group：`default`
- 默认 CLI messaging group：`cli:local`
- CLI 到 default agent 的 wiring
- `groups/default/CLAUDE.md`
- `groups/default/CLAUDE.local.md`
- `groups/default/skills/`
- `groups/default/container.json`

CLI 渠道总是开启。启动后直接在终端输入一句话并回车，就会发给默认 agent。

## 接入 Telegram

这个项目使用 Telegram long polling，不是 webhook。也就是说服务器不需要提供公网 HTTPS 回调地址，但服务器必须能访问 Telegram API。

### 1. 创建 Telegram Bot

在 Telegram 里打开 `@BotFather`，发送：

```text
/newbot
```

BotFather 会问你两个东西：

1. bot 显示名称，例如 `Andy Agent`。
2. bot username，必须全局唯一，并且通常要以 `bot` 结尾，例如 `andy_agent_bot`。

创建成功后，BotFather 会给你一个 token：

```text
123456789:ABC_xxx
```

把它写到 `.env`：

```env
TELEGRAM_BOT_TOKEN=123456789:ABC_xxx
```

这个 token 相当于 bot 的密码，不能提交到 GitHub，也不要公开截图。

### 2. 群聊 privacy mode

privacy mode 只影响 Telegram 群聊。

如果 privacy mode 开着，Telegram 可能只把命令、`@bot` 消息、回复 bot 的消息发给你的程序。普通群消息可能根本不会到达 Clawside。

如果你希望 bot 在群里看到普通消息，去 BotFather 里执行：

```text
/setprivacy
```

选择你的 bot，然后选择：

```text
Disable
```

如果你希望每次都必须 `@bot` 才响应，可以保持 privacy mode 开着，并使用：

```text
--engage-mode mention
```

如果你使用：

```text
--engage-mode mention-sticky
```

或者：

```text
--engage-mode pattern --engage-pattern "."
```

群聊里通常建议关闭 privacy mode，否则普通消息可能到不了项目。

### 3. 获取 chat_id 和 owner_user_id

把 bot 加入目标群，或者打开和 bot 的私聊，然后先发一条测试消息。

执行：

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABC_xxx"

curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
  | python -m json.tool
```

群聊里你会看到：

```json
"chat": {
  "id": -1001234567890,
  "type": "supergroup"
}
```

这个 `id` 就是：

```text
--telegram-chat-id "-1001234567890"
```

发送者信息里会有：

```json
"from": {
  "id": 123456,
  "username": "yourname"
}
```

这个项目里的 Telegram 用户 ID 格式是：

```text
telegram:<username>
```

所以如果你有 username，就填：

```text
--owner-user-id "telegram:yourname"
```

如果没有 username，才填数字：

```text
--owner-user-id "telegram:123456"
```

注意：代码里优先使用 Telegram username。你有 username 的时候，不要把 owner 写成数字 ID，否则权限可能对不上。

### 4. 把 Telegram 接到默认 agent

如果直接使用项目默认 agent：

```bash
python create_telegram_agent.py \
  --agent-id default \
  --agent-name "Andy" \
  --agent-folder default \
  --telegram-chat-id "-1001234567890" \
  --owner-user-id "telegram:yourname" \
  --engage-mode mention-sticky
```

这个脚本会创建或更新：

- `agent_groups`：agent 定义。
- `messaging_groups`：Telegram 群或私聊。
- `messaging_group_agents`：Telegram 群到 agent 的绑定关系。
- `users`：Telegram 用户。
- `user_roles`：把你授予为该 agent 的 owner。
- `agent_destinations`：让 agent 可以把消息发回 Telegram。
- `groups/default/`：默认 agent 工作目录。

然后启动：

```bash
python -m src.main
```

在 Telegram 群里先发：

```text
@andy_agent_bot 你好
```

如果你使用 `mention-sticky`，第一次 `@bot` 之后，同一个群或 topic 的后续消息可以继续进入同一个 agent 会话，不一定每次都 `@bot`。

### 私聊 Telegram bot

如果只想自己私聊使用，不需要关闭 privacy mode。

私聊时 `chat.id` 通常是正数，例如：

```text
123456789
```

推荐这样接入：

```bash
python create_telegram_agent.py \
  --agent-id default \
  --agent-name "Andy" \
  --agent-folder default \
  --telegram-chat-id "123456789" \
  --owner-user-id "telegram:yourname" \
  --engage-mode pattern \
  --engage-pattern "."
```

`pattern "."` 的意思是匹配几乎所有非空消息，适合私聊或专用 bot 群。

## 自定义 Agent

一个 agent 在项目里不是一个单独进程，而是：

```text
中心数据库里的 agent_groups 行
+ groups/<folder>/ 目录
+ container_configs 配置
+ messaging_group_agents 绑定关系
```

创建一个 writer agent，并接入 Telegram：

```bash
python create_telegram_agent.py \
  --agent-id writer \
  --agent-name "Writer" \
  --agent-folder writer \
  --telegram-chat-id "-1001234567890" \
  --owner-user-id "telegram:yourname" \
  --engage-mode mention-sticky
```

创建后会有：

```text
groups/writer/
  CLAUDE.md
  CLAUDE.local.md
  container.json
  skills/
```

你可以编辑：

```text
groups/writer/CLAUDE.md
```

来定义这个 agent 的角色、风格、规则。

`CLAUDE.local.md` 会被创建为一个可写的长期记忆文件。不过当前代码里，容器启动时只会自动读取 `CLAUDE.md`，不会自动把 `CLAUDE.local.md` 注入系统提示词。如果你希望它每轮都一定生效，需要在 `CLAUDE.md` 中要求 agent 主动读取它，或者修改 `container/agent_runner/main.py` 把它也加载进 base prompt。

## 内置 MCP 工具

内置 MCP server 在：

```text
container/agent_runner/mcp_servers/clawside.py
```

当前实现的工具：

- `send_message(text, to=None)`：发送消息。
- `send_file(path, to=None, text="", filename=None)`：发送文件。
- `edit_message(message_id, text)`：排队编辑消息。
- `ask_user_question(title, question, options, timeout=300)`：询问用户选择题。
- `schedule_task(prompt, process_after, recurrence=None, script=None)`：创建定时任务。
- `list_tasks(status=None)`：列出任务。
- `cancel_task(task_id)`：取消任务。
- `pause_task(task_id)`：暂停任务。
- `resume_task(task_id)`：恢复任务。
- `update_task(task_id, ...)`：更新任务。
- `read_file(path, offset=0, limit=200)`：读取 `/workspace` 下的文件。
- `write_file(path, content)`：写文件。
- `edit_file(path, old_string, new_string)`：替换文件内容。
- `run_bash(command, timeout_ms=30000)`：在 `/workspace` 下运行 bash。
- `load_skill(name)`：加载 skill 全文。

容器内工具的文件访问范围限制在 `/workspace` 下。容器能看到：

- `/workspace`：当前 session 目录。
- `/workspace/agent`：当前 agent group 目录。
- `/workspace/global`：如果存在，则以只读方式挂载。

## Skills

skill 是一个包含 `SKILL.md` 的目录。`SKILL.md` 由 YAML frontmatter 加正文组成。

容器启动时扫描两个目录：

- `/app/skills`：内置 skills，来自 `container/skills`，构建镜像时复制进去。
- `/workspace/agent/skills`：当前 agent 的自定义 skills，来自 `groups/<folder>/skills`。

如果自定义 skill 和内置 skill 同名，自定义 skill 会覆盖内置 skill。

示例：

```text
groups/writer/skills/research/SKILL.md
```

```markdown
---
name: research
description: Research a topic and return a concise brief
triggers:
  - on: channel_type
    value: telegram
---

# Research

Use available tools to gather facts, compare sources, and write a short brief.
```

支持的触发器：

- `on: always`：每轮都自动加载。
- `on: first_message_in_session`：session 第一轮自动加载。
- `on: channel_type` + `value: telegram`：指定渠道自动加载。

没有 triggers 的 skill 会出现在 Available Skills 索引里，agent 可以通过 `load_skill(name)` 加载全文。

## 询问用户

agent 可以调用：

```text
ask_user_question(title, question, options, timeout=300)
```

主机会把它渲染成：

- CLI：编号选项。
- Telegram：inline keyboard 按钮。

用户点击或选择后，主机会往同一个 session 的 `inbound.db` 写一条 `kind='system'` 的回答消息。MCP 工具一直轮询这条回答，直到拿到结果或超时。

## 开发命令

构建：

```bash
make build
```

如果要跑测试，先安装开发依赖：

```bash
python -m pip install pytest pytest-asyncio
```

启动：

```bash
make dev
```

测试：

```bash
make test
```

当前 Makefile 里的 test 目标假设存在 `tests/` 目录。如果你的 checkout 里没有 tests，可以先做编译检查：

```bash
python -m compileall src container/agent_runner
```

## License

MIT License。见 `LICENSE`。
