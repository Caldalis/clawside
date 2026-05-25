"""
CENTRAL_SCHEMA  —— 中心 DB（data/v2.db），每个安装一份。
INBOUND_SCHEMA  —— 每会话 inbound.db（主机写、容器只读）。
OUTBOUND_SCHEMA —— 每会话 outbound.db（容器写、主机读）。
"""

CENTRAL_SCHEMA = r"""
-- Agent 工作区：文件夹、技能、CLAUDE.md。
CREATE TABLE IF NOT EXISTS agent_groups (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  folder           TEXT NOT NULL UNIQUE,
  agent_provider   TEXT,
  created_at       TEXT NOT NULL
);

-- 平台群组/渠道。
CREATE TABLE IF NOT EXISTS messaging_groups (
  id                    TEXT PRIMARY KEY,
  channel_type          TEXT NOT NULL,
  platform_id           TEXT NOT NULL,
  name                  TEXT,
  is_group              INTEGER DEFAULT 0,
  unknown_sender_policy TEXT NOT NULL DEFAULT 'strict',
                        -- 'strict' | 'request_approval' | 'public'
  created_at            TEXT NOT NULL,
  UNIQUE(channel_type, platform_id)
);

-- 哪些 agent group 处理哪些 messaging group。
CREATE TABLE IF NOT EXISTS messaging_group_agents (
  id                     TEXT PRIMARY KEY,
  messaging_group_id     TEXT NOT NULL REFERENCES messaging_groups(id),
  agent_group_id         TEXT NOT NULL REFERENCES agent_groups(id),
  engage_mode            TEXT NOT NULL DEFAULT 'mention',
                         -- 'pattern' | 'mention' | 'mention-sticky'
  engage_pattern         TEXT,    -- 正则；engage_mode='pattern' 时必填
                                  -- '.' 表示"匹配所有消息"（总是触发）
  sender_scope           TEXT NOT NULL DEFAULT 'all',     -- 'all' | 'known'
  ignored_message_policy TEXT NOT NULL DEFAULT 'drop',    -- 'drop' | 'accumulate'
  session_mode           TEXT DEFAULT 'shared',
  priority               INTEGER DEFAULT 0,
  created_at             TEXT NOT NULL,
  UNIQUE(messaging_group_id, agent_group_id)
);

-- 用户是平台标识符，带命名空间："tg:123"、"cli:local" 等。
CREATE TABLE IF NOT EXISTS users (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  display_name TEXT,
  created_at   TEXT NOT NULL
);

-- 角色授予。权限是用户级的。
--   role ∈ {owner, admin}
--   agent_group_id 非空 —— 每个授予都限定在某个特定 group。
--   cli:local 通过 agent_groups 创建钩子在每个 group 都获得一行显式权限。
CREATE TABLE IF NOT EXISTS user_roles (
  user_id        TEXT NOT NULL REFERENCES users(id),
  role           TEXT NOT NULL,
  agent_group_id TEXT NOT NULL REFERENCES agent_groups(id),
  granted_by     TEXT REFERENCES users(id),
  granted_at     TEXT NOT NULL,
  PRIMARY KEY (user_id, role, agent_group_id)
);
CREATE INDEX IF NOT EXISTS idx_user_roles_scope ON user_roles(agent_group_id, role);

-- 在 agent group 中的非特权"已知"成员关系。
CREATE TABLE IF NOT EXISTS agent_group_members (
  user_id        TEXT NOT NULL REFERENCES users(id),
  agent_group_id TEXT NOT NULL REFERENCES agent_groups(id),
  added_by       TEXT REFERENCES users(id),
  added_at       TEXT NOT NULL,
  PRIMARY KEY (user_id, agent_group_id)
);

-- 缓存的 (user, channel) → DM messaging group 查找表。
CREATE TABLE IF NOT EXISTS user_dms (
  user_id            TEXT NOT NULL REFERENCES users(id),
  channel_type       TEXT NOT NULL,
  messaging_group_id TEXT NOT NULL REFERENCES messaging_groups(id),
  resolved_at        TEXT NOT NULL,
  PRIMARY KEY (user_id, channel_type)
);

-- 会话：一个文件夹 = 一个会话 = 运行时一个容器。
CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  agent_group_id     TEXT NOT NULL REFERENCES agent_groups(id),
  messaging_group_id TEXT REFERENCES messaging_groups(id),
  thread_id          TEXT,
  agent_provider     TEXT,
  status             TEXT DEFAULT 'active',
  container_status   TEXT DEFAULT 'stopped',
  last_active        TEXT,
  created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_group ON sessions(agent_group_id);
CREATE INDEX IF NOT EXISTS idx_sessions_lookup ON sessions(messaging_group_id, thread_id);

-- 待处理的交互式问题（ask_user_question）。
CREATE TABLE IF NOT EXISTS pending_questions (
  question_id    TEXT PRIMARY KEY,
  session_id     TEXT NOT NULL REFERENCES sessions(id),
  message_out_id TEXT NOT NULL,
  platform_id    TEXT,
  channel_type   TEXT,
  thread_id      TEXT,
  title          TEXT NOT NULL,
  options_json   TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

-- 每个 agent group 的容器运行时配置。以单个 JSON `config` 列存储，
-- 而非分散到多个有类型的列 —— 使 schema 在 agent 增加新参数（model、
-- mcp_servers、packages、mounts 等）时保持稳定。
CREATE TABLE IF NOT EXISTS container_configs (
  agent_group_id TEXT PRIMARY KEY REFERENCES agent_groups(id) ON DELETE CASCADE,
  config         TEXT NOT NULL DEFAULT '{}',
  cli_scope      TEXT NOT NULL DEFAULT 'group',
                 -- 'disabled' | 'group' | 'global'
  updated_at     TEXT NOT NULL
);

-- 发送者未被识别 / 未被允许的入站消息。
-- （Nanoclaw 中称为 `unregistered_senders`；clawside 按 Phase 1 规范
-- 重命名为 `dropped_messages`。）
CREATE TABLE IF NOT EXISTS dropped_messages (
  channel_type       TEXT NOT NULL,
  platform_id        TEXT NOT NULL,
  user_id            TEXT,
  sender_name        TEXT,
  reason             TEXT NOT NULL,
  messaging_group_id TEXT,
  agent_group_id     TEXT,
  message_count      INTEGER NOT NULL DEFAULT 1,
  first_seen         TEXT NOT NULL,
  last_seen          TEXT NOT NULL,
  PRIMARY KEY (channel_type, platform_id)
);
CREATE INDEX IF NOT EXISTS idx_dropped_messages_last_seen
  ON dropped_messages(last_seen);

-- 未知发送者的待审批记录（unknown_sender_policy='request_approval'）。
CREATE TABLE IF NOT EXISTS pending_sender_approvals (
  id                 TEXT PRIMARY KEY,
  messaging_group_id TEXT NOT NULL REFERENCES messaging_groups(id),
  agent_group_id     TEXT NOT NULL REFERENCES agent_groups(id),
  sender_identity    TEXT NOT NULL,
  sender_name        TEXT,
  original_message   TEXT NOT NULL,
  approver_user_id   TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  UNIQUE(messaging_group_id, sender_identity)
);
CREATE INDEX IF NOT EXISTS idx_pending_sender_approvals_mg
  ON pending_sender_approvals(messaging_group_id);

-- Agent destinations：每个 agent 的命名可达目标映射。
-- 同时是路由表和 ACL。只有当源 agent 被允许发送到该目标时，对应行才存在。
-- container_runner 在每次 spawn 时把这些行原样复制到 inbound.db.destinations。
CREATE TABLE IF NOT EXISTS agent_destinations (
  agent_group_id        TEXT NOT NULL REFERENCES agent_groups(id) ON DELETE CASCADE,
  name                  TEXT NOT NULL,
  display_name          TEXT,
  type                  TEXT NOT NULL,    -- 'channel' | 'agent'
  channel_type          TEXT,             -- type='channel' 时使用
  platform_id           TEXT,             -- type='channel' 时使用
  target_agent_group_id TEXT REFERENCES agent_groups(id),   -- type='agent' 时使用
  created_at            TEXT NOT NULL,
  PRIMARY KEY (agent_group_id, name)
);
CREATE INDEX IF NOT EXISTS idx_agent_dest_channel
  ON agent_destinations(channel_type, platform_id);
CREATE INDEX IF NOT EXISTS idx_agent_dest_agent
  ON agent_destinations(target_agent_group_id);
"""


# 主机所有：入站消息 + 投递跟踪 + 目标地址映射
INBOUND_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS messages_in (
  id                TEXT PRIMARY KEY,
  seq               INTEGER UNIQUE,
  kind              TEXT NOT NULL,
  timestamp         TEXT NOT NULL,
  status            TEXT DEFAULT 'pending',
  process_after     TEXT,
  recurrence        TEXT,
  series_id         TEXT,
  tries             INTEGER DEFAULT 0,
  trigger           INTEGER NOT NULL DEFAULT 1,
                    -- 0 = 累积上下文（不唤醒），1 = 唤醒 agent
  platform_id       TEXT,
  channel_type      TEXT,
  thread_id         TEXT,
  content           TEXT NOT NULL,
  source_session_id TEXT,
                    -- agent 间通信：发出消息的 session id（回程路径）
  on_wake           INTEGER NOT NULL DEFAULT 0
                    -- 1 = 仅在容器首次轮询时投递（全新启动）
);
CREATE INDEX IF NOT EXISTS idx_messages_in_series ON messages_in(series_id);

-- 主机为 messages_out 的 ID 跟踪投递结果。
CREATE TABLE IF NOT EXISTS delivered (
  message_out_id      TEXT PRIMARY KEY,
  platform_message_id TEXT,
  status              TEXT NOT NULL DEFAULT 'delivered',
  delivered_at        TEXT NOT NULL
);

-- 当前 session 的 agent 的目标地址映射。主机在每次 spawn 时重写。
CREATE TABLE IF NOT EXISTS destinations (
  name            TEXT PRIMARY KEY,
  display_name    TEXT,
  type            TEXT NOT NULL,   -- 'channel' | 'agent'
  channel_type    TEXT,            -- type='channel' 时使用
  platform_id     TEXT,            -- type='channel' 时使用
  agent_group_id  TEXT             -- type='agent' 时使用
);

-- 当前 session 的默认回复路由。单行（id=1）。
CREATE TABLE IF NOT EXISTS session_routing (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  channel_type TEXT,
  platform_id  TEXT,
  thread_id    TEXT
);
"""


# 容器所有：出站消息 + 处理确认。
OUTBOUND_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS messages_out (
  id            TEXT PRIMARY KEY,
  seq           INTEGER UNIQUE,
  in_reply_to   TEXT,
  timestamp     TEXT NOT NULL,
  deliver_after TEXT,
  recurrence    TEXT,
  kind          TEXT NOT NULL,
  platform_id   TEXT,
  channel_type  TEXT,
  thread_id     TEXT,
  content       TEXT NOT NULL
);

-- 容器在此跟踪处理状态，而不直接修改 messages_in。
-- 主机读取以了解哪些消息已被处理。
-- 容器启动时，陈旧的 'processing' 行会被清理（崩溃恢复）。
CREATE TABLE IF NOT EXISTS processing_ack (
  message_id     TEXT PRIMARY KEY,
  status         TEXT NOT NULL,
  status_changed TEXT NOT NULL
);

-- 容器所有的持久化 key/value 状态。
-- 用途：历史记录（chat completions），以及其他长存状态。
-- 由 /clear 命令清空。
CREATE TABLE IF NOT EXISTS session_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 当前工具执行中的状态。单行（id=1）。容器在工具开始时写入，结束时清空。
-- 主机在 sweep 中读取，以便在长时工具（声明超时 > 60s）运行时放宽
-- stuck 容忍度。
CREATE TABLE IF NOT EXISTS container_state (
  id                       INTEGER PRIMARY KEY CHECK (id = 1),
  current_tool             TEXT,
  tool_declared_timeout_ms INTEGER,
  tool_started_at          TEXT,
  updated_at               TEXT NOT NULL
);
"""
