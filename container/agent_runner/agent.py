from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Optional


def _log(msg: str) -> None:
    print(f"[agent] {msg}", file=sys.stderr, flush=True)


async def run_agent(
    messages: list[dict],
    mcp_manager: Any,
    client: Any,
    model: str,
    max_tool_rounds: int = 20,
) -> tuple[Optional[str], list[dict]]:
    """
    运行一次 agent
    返回 final_text, updated_history
    """
    for _ in range(max_tool_rounds):
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if mcp_manager.openai_tools:
            kwargs["tools"] = mcp_manager.openai_tools

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        msg_dict = msg.model_dump(exclude_none=True)
        messages.append(msg_dict)

        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return msg.content, messages

        # 并行派发所有工具调用。
        async def _invoke(tc):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                return tc.id, f"Error: failed to parse arguments: {e!r}"
            result = await mcp_manager.call(tc.function.name, args)
            return tc.id, result

        invocations = await asyncio.gather(*(_invoke(tc) for tc in tool_calls))
        for tc_id, content in invocations:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": content if isinstance(content, str) else str(content),
                }
            )

    _log(f"max_tool_rounds ({max_tool_rounds}) exceeded — returning None")
    return None, messages
