from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


CLAWSIDE_SERVER_NAME = "clawside"


def _log(msg: str) -> None:
    print(f"[mcp-manager] {msg}", file=sys.stderr, flush=True)

def _tool_to_openai(tool: Any) -> dict[str, Any]:
    """把 MCP 工具描述符转换为 OpenAI function-tool schema。"""
    schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": getattr(tool, "description", "") or "",
            "parameters": schema,
        },
    }

class MCPManager:



    def __init__(self) -> None:
        self.openai_tools: list[dict] = []
        self._stack: Optional[AsyncExitStack] = None
        self._tool_to_session: dict[str, ClientSession] = {}
        self._sessions: dict[str, ClientSession] = {}

    async def __aenter__(self) -> "MCPManager":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, tb)
            self._stack = None

    async def start(self, servers: dict[str, dict]) -> None:

        if self._stack is None:
            raise RuntimeError("MCPManager.start called outside `async with` block")

        for name, spec in servers.items():
            try:
                await self._start_one(name, spec)
            except Exception as e:
                if name == CLAWSIDE_SERVER_NAME:
                    _log(f"clawside MCP server failed to start: {e!r}")
                    raise
                _log(f"MCP server {name!r} failed to start, continuing: {e!r}")

    async def _start_one(self, name: str, spec: dict) -> None:
        command = spec.get("command")
        if not command:
            raise ValueError(f"MCP server {name!r} has no command")
        args = spec.get("args") or []
        env = spec.get("env")

        params = StdioServerParameters(command=command, args=list(args), env=env)

        read, write = await self._stack.enter_async_context(stdio_client(params))  # type: ignore[union-attr]
        session = await self._stack.enter_async_context(ClientSession(read, write))  # type: ignore[union-attr]
        await session.initialize()

        tools_resp = await session.list_tools()
        tools = getattr(tools_resp, "tools", None) or []
        for tool in tools:
            if tool.name in self._tool_to_session:
                _log(f"duplicate tool name {tool.name!r}; later server wins")
            self._tool_to_session[tool.name] = session
            self.openai_tools.append(_tool_to_openai(tool))

        self._sessions[name] = session
        _log(f"started MCP server {name!r} with {len(tools)} tool(s)")

    async def call(self, tool_name: str, arguments: dict) -> str:
        session = self._tool_to_session.get(tool_name)
        if session is None:
            return f"Error: unknown tool {tool_name!r}"
        try:
            result = await session.call_tool(tool_name, arguments)
        except Exception as e:
            return f"Error: tool {tool_name!r} raised {e!r}"

        parts: list[str] = []
        content = getattr(result, "content", None) or []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
                continue
            try:
                parts.append(json.dumps(item, default=str))
            except Exception:
                parts.append(str(item))

        if getattr(result, "isError", False):
            return "Error: " + ("\n".join(parts) if parts else "tool returned error")
        return "\n".join(parts)

    async def close(self) -> None:

        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self.openai_tools.clear()
            self._tool_to_session.clear()
            self._sessions.clear()
