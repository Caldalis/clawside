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
Clawside is a Python agent orchestration system. It is made of a long-running
host process and per-session Docker agent containers: the host receives CLI or
Telegram messages, routes them to a specific agent, writes them into the session
queue, and starts the container; the container runs an OpenAI-compatible
ReAct/tool-use loop and writes replies, scheduled tasks, user questions, and
other results back into queues that the host can read.

Current project capabilities:

- Host side: Python 3.11+, asyncio, SQLite, Docker.
- Container side: Python, OpenAI Chat Completions, MCP stdio.
- Channels: local CLI and Telegram long polling.
- Agents: represented by `agent_group`; each agent group maps to a
  `groups/<folder>/` directory.
- Runtime queues: each session has its own `inbound.db` and `outbound.db`.
- Built-in MCP tools: send messages, send files, read/write files, run bash,
  schedule tasks, ask users questions, and load skills.

## How It Works

The overall flow is:

```text
User
  -> ChannelAdapter, such as CLI or Telegram
  -> src.router.route_inbound()
  -> session inbound.db
  -> Docker agent container
  -> OpenAI Chat Completions + MCP tools
  -> session outbound.db
  -> src.delivery
  -> ChannelAdapter.deliver()
  -> User
```

The project intentionally splits data into three categories:

- `data/v2.db`: central database for global config, agents, group bindings,
  permissions, session indexes, pending questions, and related records.
- `inbound.db`: one per session; written by the host and read by the container.
- `outbound.db`: one per session; written by the container and read by the host.

Each session directory looks like this:

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

Key invariants:

- `inbound.db` is written only by the host.
- `outbound.db` is written only by the container.
- The host uses `processing_ack` and `.heartbeat` to decide whether the
  container is processing, stuck, or should be retried.
- The host writes `messages_in` with even `seq` values.
- The container writes `messages_out` with odd `seq` values.

## Requirements

- Python 3.11+
- Docker
- An OpenAI-compatible API key
- A Telegram bot token if you want Telegram integration

Install dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install \
  "python-telegram-bot>=21.0" \
  "structlog>=24.0" \
  "python-dotenv>=1.0" \
  "croniter>=2.0"
```

Run commands from the project root. The host process is started with
`python -m src.main`; editable package install is not required.

Build the agent image:

```bash
bash container/build.sh
```

By default, this builds:

```text
clawside-agent:latest
```

If `CONTAINER_IMAGE` is set, that image name is used instead.

## Configure `.env`

Create `.env` in the project root:

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

# Optional: required only for Telegram
TELEGRAM_BOT_TOKEN=123456789:ABC_xxx
```

Main environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | API key passed into agent containers | none |
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL | `https://api.openai.com/v1` |
| `DEFAULT_MODEL` | Default model | `gpt-4o` |
| `CONTAINER_IMAGE` | Agent container image | `clawside-agent:latest` |
| `CONTAINER_RUNTIME` | Container runtime | `docker` |
| `TELEGRAM_BOT_TOKEN` | Enables the Telegram adapter when present | none |
| `TIMEZONE` | User timezone and scheduled-task timezone | `UTC` |
| `DATA_DIR` | Central DB and session DB directory | `./data` |
| `GROUPS_DIR` | Agent group directory | `./groups` |
| `ASSISTANT_NAME` | Default agent name | `Andy` |
| `CLI_SOCKET_PATH` | Management socket path | `data/clawside.sock` |

Runtime parameters:

| Variable | Purpose | Default |
|---|---|---|
| `ACTIVE_POLL_MS` | Delivery poll interval for running sessions | `1000` |
| `SWEEP_POLL_MS` | Host sweep interval | `60000` |
| `ABSOLUTE_CEILING_MS` | Max stale heartbeat age before a container can be killed | `1800000` |
| `CLAIM_STUCK_MS` | Processing-claim stuck threshold | `60000` |
| `MAX_TRIES` | Max message retry count | `5` |
| `BACKOFF_BASE_MS` | Retry backoff base | `5000` |
| `MAX_DELIVERY_ATTEMPTS` | Max outbound delivery attempts | `3` |

## Run Locally

Start the host process:

```bash
python -m src.main
```

or:

```bash
make dev
```

On first start, the host automatically creates:

- `data/v2.db`
- default user `cli:local`
- default agent group: `default`
- default CLI messaging group: `cli:local`
- CLI-to-default-agent wiring
- `groups/default/CLAUDE.md`
- `groups/default/CLAUDE.local.md`
- `groups/default/skills/`
- `groups/default/container.json`

The CLI channel is always enabled. After startup, type a message in the terminal
and press Enter; it will be sent to the default agent.

## Telegram Integration

This project uses Telegram long polling, not webhooks. The server does not need
to expose a public HTTPS callback URL, but it must be able to reach the Telegram
API.

### 1. Create a Telegram Bot

In Telegram, open `@BotFather` and send:

```text
/newbot
```

BotFather will ask for two things:

1. The bot display name, for example `Andy Agent`.
2. The bot username, which must be globally unique and usually ends with `bot`,
   for example `andy_agent_bot`.

After creation, BotFather gives you a token:

```text
123456789:ABC_xxx
```

Put it in `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:ABC_xxx
```

This token is effectively the bot password. Do not commit it to GitHub and do
not publish screenshots of it.

### 2. Group Privacy Mode

Privacy mode only affects Telegram group chats.

When privacy mode is enabled, Telegram may only send commands, `@bot` messages,
and replies to the bot to your program. Plain group messages may never reach
Clawside.

If you want the bot to see normal group messages, run this in BotFather:

```text
/setprivacy
```

Select your bot, then choose:

```text
Disable
```

If you want the bot to respond only when it is explicitly mentioned, you can
keep privacy mode enabled and use:

```text
--engage-mode mention
```

If you use:

```text
--engage-mode mention-sticky
```

or:

```text
--engage-mode pattern --engage-pattern "."
```

then it is usually recommended to disable privacy mode in group chats,
otherwise plain messages may never reach the project.

### 3. Get `chat_id` and `owner_user_id`

Add the bot to the target group, or open a private chat with the bot, then send
a test message first.

Run:

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABC_xxx"

curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
  | python -m json.tool
```

In a group, you will see:

```json
"chat": {
  "id": -1001234567890,
  "type": "supergroup"
}
```

This `id` is:

```text
--telegram-chat-id "-1001234567890"
```

The sender block will include:

```json
"from": {
  "id": 123456,
  "username": "yourname"
}
```

Telegram user IDs in this project use this format:

```text
telegram:<username>
```

So if you have a username, use:

```text
--owner-user-id "telegram:yourname"
```

If there is no username, use the numeric ID:

```text
--owner-user-id "telegram:123456"
```

Note: the code prefers Telegram usernames. If you have a username, do not write
the owner as the numeric ID, or the permission check may not match.

### 4. Connect Telegram to the Default Agent

If you want to use the default project agent directly:

```bash
python create_telegram_agent.py \
  --agent-id default \
  --agent-name "Andy" \
  --agent-folder default \
  --telegram-chat-id "-1001234567890" \
  --owner-user-id "telegram:yourname" \
  --engage-mode mention-sticky
```

This script creates or updates:

- `agent_groups`: agent definition.
- `messaging_groups`: Telegram group or private chat.
- `messaging_group_agents`: binding from Telegram group to agent.
- `users`: Telegram user.
- `user_roles`: grants you owner access to this agent.
- `agent_destinations`: lets the agent send messages back to Telegram.
- `groups/default/`: default agent working directory.

Then start:

```bash
python -m src.main
```

In the Telegram group, first send:

```text
@andy_agent_bot hello
```

If you use `mention-sticky`, after the first `@bot`, later messages in the same
group or topic can continue into the same agent session without mentioning the
bot every time.

### Private Telegram Bot Chat

If you only want to use the bot through a private chat, you do not need to
disable privacy mode.

In private chat, `chat.id` is usually a positive number, for example:

```text
123456789
```

Recommended setup:

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

`pattern "."` means it matches almost any non-empty message, which works well
for private chats or dedicated bot groups.

## Custom Agents

An agent in this project is not a standalone process. It is:

```text
an agent_groups row in the central database
+ groups/<folder>/ directory
+ container_configs config
+ messaging_group_agents binding
```

Create a writer agent and connect it to Telegram:

```bash
python create_telegram_agent.py \
  --agent-id writer \
  --agent-name "Writer" \
  --agent-folder writer \
  --telegram-chat-id "-1001234567890" \
  --owner-user-id "telegram:yourname" \
  --engage-mode mention-sticky
```

After creation, you will have:

```text
groups/writer/
  CLAUDE.md
  CLAUDE.local.md
  container.json
  skills/
```

You can edit:

```text
groups/writer/CLAUDE.md
```

to define the agent's role, style, and rules.

`CLAUDE.local.md` is created as a writable long-term memory file. In the current
code, the container startup only automatically reads `CLAUDE.md`; it does not
automatically inject `CLAUDE.local.md` into the system prompt. If you want it to
be guaranteed in context every turn, instruct the agent in `CLAUDE.md` to read
it, or modify `container/agent_runner/main.py` to load it into the base prompt.

## Built-in MCP Tools

The built-in MCP server is:

```text
container/agent_runner/mcp_servers/clawside.py
```

Currently implemented tools:

- `send_message(text, to=None)`: send a message.
- `send_file(path, to=None, text="", filename=None)`: send a file.
- `edit_message(message_id, text)`: queue a message edit.
- `ask_user_question(title, question, options, timeout=300)`: ask the user a
  multiple-choice question.
- `schedule_task(prompt, process_after, recurrence=None, script=None)`: create
  a scheduled task.
- `list_tasks(status=None)`: list tasks.
- `cancel_task(task_id)`: cancel a task.
- `pause_task(task_id)`: pause a task.
- `resume_task(task_id)`: resume a task.
- `update_task(task_id, ...)`: update a task.
- `read_file(path, offset=0, limit=200)`: read a file under `/workspace`.
- `write_file(path, content)`: write a file.
- `edit_file(path, old_string, new_string)`: replace file content.
- `run_bash(command, timeout_ms=30000)`: run bash under `/workspace`.
- `load_skill(name)`: load the full skill text.

Container tools are limited to `/workspace`. The container can see:

- `/workspace`: current session directory.
- `/workspace/agent`: current agent group directory.
- `/workspace/global`: mounted read-only if it exists.

## Skills

A skill is a directory containing `SKILL.md`. `SKILL.md` is made of YAML
frontmatter plus instruction body.

The container scans two directories on startup:

- `/app/skills`: built-in skills from `container/skills`, copied into the image
  during build.
- `/workspace/agent/skills`: custom skills for the current agent, from
  `groups/<folder>/skills`.

If a custom skill has the same name as a built-in skill, the custom skill
overrides the built-in one.

Example:

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

Supported triggers:

- `on: always`: auto-load every turn.
- `on: first_message_in_session`: auto-load on the first session turn.
- `on: channel_type` + `value: telegram`: auto-load for a specific channel.

Skills without triggers appear in the Available Skills index, and the agent can
load the full text with `load_skill(name)`.

## Asking the User

The agent can call:

```text
ask_user_question(title, question, options, timeout=300)
```

The host renders it as:

- CLI: numbered options.
- Telegram: inline keyboard buttons.

After the user clicks or selects an option, the host writes a `kind='system'`
answer message into the same session's `inbound.db`. The MCP tool keeps polling
for that answer until it arrives or times out.

## Development Commands

Build:

```bash
make build
```

Install development dependencies before running tests:

```bash
python -m pip install pytest pytest-asyncio
```

Start:

```bash
make dev
```

Test:

```bash
make test
```

The current Makefile test target assumes a `tests/` directory exists. If your
checkout does not have tests, run a compile check first:

```bash
python -m compileall src container/agent_runner
```

## License

MIT License. See `LICENSE`.
