from __future__ import annotations

import json
import os
from typing import Iterable

from src.config import get_config
from src.db.agent_groups import AgentGroup, list_agent_groups
from src.db.container_configs import get_container_config
from src.log import log


_DEFAULT_CLAUDE_MD = """# Agent

You are a personal assistant running inside a per-session container.

Reply concisely. Use the destinations map at the bottom of your prompt to
send messages; the default destination is the channel that woke you.
"""


def init_group_filesystem(group: AgentGroup) -> None:
    cfg = get_config()
    group_dir = os.path.join(cfg.groups_dir_abs, group.folder)
    os.makedirs(group_dir, exist_ok=True)

    claude_md = os.path.join(group_dir, "CLAUDE.md")
    if not os.path.exists(claude_md):
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_CLAUDE_MD)

    claude_local_md = os.path.join(group_dir, "CLAUDE.local.md")
    if not os.path.exists(claude_local_md):
        with open(claude_local_md, "w", encoding="utf-8") as f:
            f.write("")

    cc = get_container_config(group.id)
    container_blob = cc.config if cc is not None else {}
    cli_scope = cc.cli_scope if cc is not None else "group"
    container_blob_out = dict(container_blob)
    container_blob_out["cli_scope"] = cli_scope
    container_json = os.path.join(group_dir, "container.json")
    with open(container_json, "w", encoding="utf-8") as f:
        json.dump(container_blob_out, f, indent=2)
        f.write("\n")

    skills_dir = os.path.join(group_dir, "skills")
    os.makedirs(skills_dir, exist_ok=True)

    log.debug("group_filesystem_ready", group_id=group.id, folder=group.folder)


def reconcile_all_groups(groups: Iterable[AgentGroup] | None = None) -> None:

    rows = list(groups) if groups is not None else list_agent_groups()
    for g in rows:
        try:
            init_group_filesystem(g)
        except Exception as e:
            log.error(
                "group_filesystem_init_failed",
                group_id=g.id, folder=g.folder, err=str(e),
            )
