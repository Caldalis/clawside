import argparse
import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.config import get_config
from src.db.connection import init_db, close_db
from src.db.migrations import run_migrations
from src.bootstrap import ensure_default_setup
from src.group_init import init_group_filesystem
from src.db.agent_groups import AgentGroup, get_agent_group, create_agent_group
from src.db.messaging_groups import (
    MessagingGroup,
    get_messaging_group_by_platform,
    create_messaging_group,
)


def now():
    return datetime.now(timezone.utc).isoformat()


parser = argparse.ArgumentParser()
parser.add_argument("--agent-id", required=True)
parser.add_argument("--agent-name", required=True)
parser.add_argument("--agent-folder", required=True)
parser.add_argument("--telegram-chat-id", required=True)
parser.add_argument("--owner-user-id", required=True)
parser.add_argument("--engage-mode", default="mention-sticky")
parser.add_argument("--engage-pattern", default=None)
args = parser.parse_args()

load_dotenv()

cfg = get_config()
db = init_db(os.path.join(cfg.data_dir_abs, "v2.db"))
run_migrations(db)
ensure_default_setup(db)

if get_agent_group(args.agent_id) is None:
    create_agent_group(
        AgentGroup(
            id=args.agent_id,
            name=args.agent_name,
            folder=args.agent_folder,
            agent_provider="openai",
            created_at=now(),
        )
    )

group = get_agent_group(args.agent_id)
init_group_filesystem(group)

config_blob = {
    "provider": "openai",
    "model": os.environ.get("DEFAULT_MODEL", "gpt-4o"),
    "image": os.environ.get("CONTAINER_IMAGE", "clawside-agent:latest"),
    "assistant_name": args.agent_name,
    "packages": [],
    "mcp_servers": [],
    "skills": "all",
    "max_messages_per_prompt": 10,
}

db.execute(
    """
    INSERT INTO container_configs (agent_group_id, config, cli_scope, updated_at)
    VALUES (?, ?, 'group', ?)
    ON CONFLICT(agent_group_id) DO UPDATE SET
      config = excluded.config,
      cli_scope = excluded.cli_scope,
      updated_at = excluded.updated_at
    """,
    (args.agent_id, json.dumps(config_blob), now()),
)

kind = args.owner_user_id.split(":", 1)[0]
db.execute(
    """
    INSERT OR IGNORE INTO users (id, kind, display_name, created_at)
    VALUES (?, ?, ?, ?)
    """,
    (args.owner_user_id, kind, args.owner_user_id, now()),
)

db.execute(
    """
    INSERT OR IGNORE INTO user_roles
    (user_id, role, agent_group_id, granted_by, granted_at)
    VALUES (?, 'owner', ?, ?, ?)
    """,
    (args.owner_user_id, args.agent_id, args.owner_user_id, now()),
)

mg = get_messaging_group_by_platform("telegram", args.telegram_chat_id)
if mg is None:
    mg_id = "mg:telegram:" + args.telegram_chat_id
    create_messaging_group(
        MessagingGroup(
            id=mg_id,
            channel_type="telegram",
            platform_id=args.telegram_chat_id,
            name="Telegram " + args.telegram_chat_id,
            is_group=1 if args.telegram_chat_id.startswith("-") else 0,
            unknown_sender_policy="strict",
            created_at=now(),
        )
    )
else:
    mg_id = mg.id

wiring_id = f"wire:{mg_id}:{args.agent_id}"
db.execute(
    """
    INSERT INTO messaging_group_agents (
      id, messaging_group_id, agent_group_id,
      engage_mode, engage_pattern, sender_scope, ignored_message_policy,
      session_mode, priority, created_at
    ) VALUES (?, ?, ?, ?, ?, 'all', 'drop', 'shared', 0, ?)
    ON CONFLICT(messaging_group_id, agent_group_id) DO UPDATE SET
      engage_mode = excluded.engage_mode,
      engage_pattern = excluded.engage_pattern,
      sender_scope = excluded.sender_scope,
      ignored_message_policy = excluded.ignored_message_policy,
      session_mode = excluded.session_mode,
      priority = excluded.priority
    """,
    (
        wiring_id,
        mg_id,
        args.agent_id,
        args.engage_mode,
        args.engage_pattern,
        now(),
    ),
)

db.execute(
    """
    INSERT INTO agent_destinations (
      agent_group_id, name, display_name, type,
      channel_type, platform_id, target_agent_group_id, created_at
    ) VALUES (?, 'telegram', 'Telegram', 'channel', 'telegram', ?, NULL, ?)
    ON CONFLICT(agent_group_id, name) DO UPDATE SET
      display_name = excluded.display_name,
      type = excluded.type,
      channel_type = excluded.channel_type,
      platform_id = excluded.platform_id,
      target_agent_group_id = excluded.target_agent_group_id
    """,
    (args.agent_id, args.telegram_chat_id, now()),
)

db.commit()
close_db()

print("created/updated agent:", args.agent_id)
print("folder: groups/" + args.agent_folder)
print("telegram messaging_group:", mg_id)
print("wiring:", wiring_id)
print("owner:", args.owner_user_id)

"""
  然后执行：

  source .venv/bin/activate

  python ops/create_telegram_agent.py \
    --agent-id writer \
    --agent-name "Writer" \
    --agent-folder writer \
    --telegram-chat-id "-1001234567890" \
    --owner-user-id "telegram:yourname" \
    --engage-mode mention-sticky
    
或者使用默认 agent 也就是（groups/default）

  python ops/create_telegram_agent.py \
    --agent-id default \
    --agent-name "Andy" \
    --agent-folder default \
    --telegram-chat-id "-1001234567890" \
    --owner-user-id "telegram:yourname" \
    --engage-mode mention-sticky
"""