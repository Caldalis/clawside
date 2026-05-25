from __future__ import annotations

import asyncio
import os
import sys

from openai import AsyncOpenAI

from agent_runner.config import load_container_config
from agent_runner.mcp_manager import MCPManager
from agent_runner.skill_loader import SkillRegistry
from agent_runner import poll_loop


BUILTIN_SKILLS_DIR = "/app/skills"
USER_SKILLS_DIR = "/workspace/agent/skills"
CLAUDE_MD_PATH = "/workspace/agent/CLAUDE.md"


def _log(msg: str) -> None:
    print(f"[main] {msg}", file=sys.stderr, flush=True)

def _read_base_prompt() -> str:
    try:
        with open(CLAUDE_MD_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except OSError as e:
        _log(f"could not read {CLAUDE_MD_PATH}: {e!r}")
        return ""

def _build_servers_dict(container_servers: dict) -> dict[str, dict]:

    builtin = {
        "command": "python",
        "args": ["-m", "agent_runner.mcp_servers.clawside"],
    }
    servers: dict[str, dict] = {"clawside": builtin}
    for name, spec in (container_servers or {}).items():
        if name == "clawside":
            _log("ignoring user-defined 'clawside' server (reserved name)")
            continue
        servers[name] = spec
    return servers


async def amain() -> None:
    config = load_container_config()
    base_prompt = _read_base_prompt()
    registry = SkillRegistry([BUILTIN_SKILLS_DIR, USER_SKILLS_DIR])
    servers = _build_servers_dict(config.mcp_servers)

    client = AsyncOpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )

    async with MCPManager() as mgr:
        await mgr.start(servers)
        await poll_loop.run(mgr, client, config, registry, base_prompt)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        _log("interrupted")


if __name__ == "__main__":
    main()
