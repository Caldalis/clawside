from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import get_config
from src.db.agent_groups import get_agent_group
from src.db.container_configs import get_container_config
from src.log import log


@dataclass
class ContainerConfig:

    agent_group_id: str
    provider: str = "openai"
    model: str = "gpt-4o"
    image_tag: str = "clawside-agent:latest"
    assistant_name: str = "Andy"
    packages: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    additional_mounts: list[dict[str, Any]] = field(default_factory=list)
    skills: Any = "all"           # 'all' 或 list[str]
    max_messages_per_prompt: int = 10
    cli_scope: str = "group"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "agent_group_id": self.agent_group_id,
            "provider": self.provider,
            "model": self.model,
            "image": self.image_tag,
            "assistant_name": self.assistant_name,
            "packages": list(self.packages),
            "mcp_servers": list(self.mcp_servers),
            "additional_mounts": list(self.additional_mounts),
            "skills": self.skills,
            "max_messages_per_prompt": self.max_messages_per_prompt,
            "cli_scope": self.cli_scope,
        }

def _coerce_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    return []

def _from_blob(agent_group_id: str, blob: dict[str, Any], cli_scope: str) -> ContainerConfig:
    cfg = get_config()
    cc = ContainerConfig(agent_group_id=agent_group_id)

    cc.provider = str(blob.get("provider") or "openai")
    cc.model = str(blob.get("model") or cfg.default_model)

    cc.image_tag = str(blob.get("image") or blob.get("image_tag") or cfg.container_image)
    cc.assistant_name = str(blob.get("assistant_name") or cfg.assistant_name)
    cc.packages = _coerce_list(blob.get("packages"))
    cc.mcp_servers = _coerce_list(blob.get("mcp_servers"))
    cc.additional_mounts = _coerce_list(blob.get("additional_mounts"))

    skills = blob.get("skills", "all")
    cc.skills = skills if (skills == "all" or isinstance(skills, list)) else "all"

    raw_max = blob.get("max_messages_per_prompt", 10)
    try:
        cc.max_messages_per_prompt = int(raw_max)
    except (TypeError, ValueError):
        cc.max_messages_per_prompt = 10

    cc.cli_scope = cli_scope or "group"
    return cc


def materialize_container_json(agent_group_id: str) -> ContainerConfig:
    cfg = get_config()
    row = get_container_config(agent_group_id)
    blob = row.config if row is not None else {}
    cli_scope = row.cli_scope if row is not None else "group"

    container_config = _from_blob(agent_group_id, blob, cli_scope)

    ag = get_agent_group(agent_group_id)
    if ag is None:
        log.warn(
            "container_json_skip_no_group",
            agent_group_id=agent_group_id,
        )
        return container_config

    group_dir = os.path.join(cfg.groups_dir_abs, ag.folder)
    os.makedirs(group_dir, exist_ok=True)
    out_path = os.path.join(group_dir, "container.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(container_config.to_json_dict(), f, indent=2)
            f.write("\n")
    except OSError as e:
        log.warn(
            "container_json_write_failed",
            agent_group_id=agent_group_id, path=out_path, err=str(e),
        )

    return container_config
