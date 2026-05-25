from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any


CONTAINER_JSON_PATH = os.environ.get(
    "CLAWSIDE_CONTAINER_JSON", "/workspace/agent/container.json"
)



@dataclass
class ContainerConfig:
    provider: str = "openai"
    model: str = "gpt-4o"
    assistant_name: str = "Andy"
    agent_group_id: str = ""
    max_messages_per_prompt: int = 10
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    additional_mounts: list[dict[str, Any]] = field(default_factory=list)

def _log(msg: str) -> None:
    print(f"[config] {msg}", file=sys.stderr, flush=True)

def load_container_config(path: str = CONTAINER_JSON_PATH) -> ContainerConfig:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        _log(f"container.json not found at {path}; using defaults")
        return ContainerConfig()
    except (OSError, json.JSONDecodeError) as e:
        _log(f"failed to read {path}: {e!r}; using defaults")
        return ContainerConfig()

    if not isinstance(data, dict):
        return ContainerConfig()

    cfg = ContainerConfig()
    cfg.provider = str(data.get("provider") or "openai")
    cfg.model = str(data.get("model") or os.environ.get("DEFAULT_MODEL") or "gpt-4o")
    cfg.assistant_name = str(
        data.get("assistant_name") or os.environ.get("ASSISTANT_NAME") or "Andy"
    )
    cfg.agent_group_id = str(data.get("agent_group_id") or "")
    try:
        cfg.max_messages_per_prompt = int(data.get("max_messages_per_prompt") or 10)
    except (TypeError, ValueError):
        cfg.max_messages_per_prompt = 10

    raw_servers = data.get("mcp_servers")
    if isinstance(raw_servers, dict):
        # 映射形式：{name: {command, args, env?}}
        cfg.mcp_servers = {k: v for k, v in raw_servers.items() if isinstance(v, dict)}
    elif isinstance(raw_servers, list):
        # 列表形式：[{name, command, args, env?}, ...]
        cfg.mcp_servers = {}
        for item in raw_servers:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            cfg.mcp_servers[name] = {
                k: v for k, v in item.items() if k != "name"
            }

    raw_mounts = data.get("additional_mounts")
    cfg.additional_mounts = raw_mounts if isinstance(raw_mounts, list) else []
    return cfg
