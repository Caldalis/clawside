from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.config import get_config
from src.db.agent_groups import AgentGroup, create_agent_group, get_agent_group
from src.db.agent_destinations import add_destination, list_destinations_for_group
from src.db.container_configs import get_container_config, upsert_container_config
from src.db.messaging_groups import (
    MessagingGroup,
    MessagingGroupAgent,
    create_messaging_group,
    create_messaging_group_agent,
    get_messaging_group_agents,
    get_messaging_group_by_platform,
)
from src.db.users import get_user, upsert_user
from src.group_init import init_group_filesystem
from src.log import log


CLI_LOCAL_USER_ID = "cli:local"
DEFAULT_AGENT_GROUP_ID = "default"
DEFAULT_AGENT_GROUP_FOLDER = "default"
DEFAULT_MESSAGING_GROUP_ID = "cli:local"
DEFAULT_WIRING_ID = "wire:cli:local:default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_default_setup(_db: sqlite3.Connection | None = None) -> None:
    cfg = get_config()

    if get_user(CLI_LOCAL_USER_ID) is None:
        upsert_user(CLI_LOCAL_USER_ID, kind="cli", display_name="CLI (local)")
        log.info("bootstrap_user_created", id=CLI_LOCAL_USER_ID)

    default_group = get_agent_group(DEFAULT_AGENT_GROUP_ID)
    if default_group is None:
        default_group = AgentGroup(
            id=DEFAULT_AGENT_GROUP_ID,
            name=cfg.assistant_name,
            folder=DEFAULT_AGENT_GROUP_FOLDER,
            agent_provider="openai",
            created_at=_now(),
        )
        create_agent_group(default_group)
        log.info("bootstrap_agent_group_created", id=DEFAULT_AGENT_GROUP_ID)

    if get_container_config(DEFAULT_AGENT_GROUP_ID) is None:
        config_blob = {
            "provider": "openai",
            "model": cfg.default_model,
            "image": cfg.container_image,
            "assistant_name": cfg.assistant_name,
            "packages": [],
            "mcp_servers": [],
        }
        upsert_container_config(DEFAULT_AGENT_GROUP_ID, config_blob, cli_scope="group")
        log.info("bootstrap_container_config_created", id=DEFAULT_AGENT_GROUP_ID)


    if get_messaging_group_by_platform("cli", "local") is None:
        mg = MessagingGroup(
            id=DEFAULT_MESSAGING_GROUP_ID,
            channel_type="cli",
            platform_id="local",
            name="CLI",
            is_group=0,
            unknown_sender_policy="strict",
            created_at=_now(),
        )
        create_messaging_group(mg)
        log.info("bootstrap_messaging_group_created", id=DEFAULT_MESSAGING_GROUP_ID)



    existing_wirings = get_messaging_group_agents(DEFAULT_MESSAGING_GROUP_ID)
    has_default_wire = any(
        w.agent_group_id == DEFAULT_AGENT_GROUP_ID for w in existing_wirings
    )
    if not has_default_wire:
        wiring = MessagingGroupAgent(
            id=DEFAULT_WIRING_ID,
            messaging_group_id=DEFAULT_MESSAGING_GROUP_ID,
            agent_group_id=DEFAULT_AGENT_GROUP_ID,
            engage_mode="pattern",
            engage_pattern=".",
            sender_scope="all",
            ignored_message_policy="drop",
            session_mode="shared",
            priority=0,
            created_at=_now(),
        )
        create_messaging_group_agent(wiring)
        log.info("bootstrap_wiring_created", id=DEFAULT_WIRING_ID)

    dest_names = {d["name"] for d in list_destinations_for_group(DEFAULT_AGENT_GROUP_ID)}
    if "cli" not in dest_names:
        add_destination(
            DEFAULT_AGENT_GROUP_ID,
            name="cli",
            type="channel",
            channel_type="cli",
            platform_id="local",
            display_name="CLI",
        )
        log.info("bootstrap_destination_created", agent_group_id=DEFAULT_AGENT_GROUP_ID, name="cli")

    fresh = get_agent_group(DEFAULT_AGENT_GROUP_ID)
    if fresh is not None:
        init_group_filesystem(fresh)
