from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from src.channels.adapter import (
    ChannelAdapter,
    InboundEvent,
    InboundMessage,
    OutboundFile,
    get_action_handler,
)
from src.log import log

if TYPE_CHECKING:
    from src.config import Config


CLI_USER_ID = "cli:local"
CLI_PLATFORM_ID = "local"


class CLIAdapter(ChannelAdapter):
    channel_type = "cli"
    supports_threads = False
    def __init__(self, cfg: "Config") -> None:
        self._cfg = cfg
        self._reader_task: Optional[asyncio.Task] = None

        self._pending_question: Optional[dict] = None


    async def start(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            return
        self._reader_task = asyncio.create_task(self._read_stdin(), name="cli-stdin")
        print("CLI adapter ready. Type and press enter to send.", file=sys.stderr, flush=True)

    async def shutdown(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
        self._reader_task = None

    async def _read_stdin(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warn("cli_stdin_read_failed", err=str(e))
                return
            if line == "":
                # EOF：stdin 已关闭（例如管道输入结束）。
                log.info("cli_stdin_eof")
                return
            text = line.rstrip("\r\n")
            if not text:
                continue
            await self._dispatch_line(text)

    async def _dispatch_line(self, text: str) -> None:
        if self._pending_question is not None:
            await self._handle_question_pick(text)
            return

        content = {
            "text": text,
            "sender": "you",
            "sender_id": CLI_USER_ID,
        }
        message = InboundMessage(
            id=f"cli-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{uuid.uuid4().hex[:6]}",
            kind="chat",
            content=json.dumps(content),
            timestamp=datetime.now(timezone.utc).isoformat(),
            is_mention=False,
            is_group=False,
        )
        event = InboundEvent(
            channel_type=self.channel_type,
            platform_id=CLI_PLATFORM_ID,
            thread_id=None,
            message=message,
        )

        from src.router import route_inbound
        try:
            await route_inbound(event)
        except Exception as e:
            log.error("cli_route_inbound_failed", err=str(e))

    async def _handle_question_pick(self, text: str) -> None:
        pending = self._pending_question
        if pending is None:
            return
        options = pending["options"]
        try:
            idx = int(text.strip())
        except ValueError:
            print(f"[Agent] Pick a number 1..{len(options)}", file=sys.stdout, flush=True)
            return
        if idx < 1 or idx > len(options):
            print(f"[Agent] Pick a number 1..{len(options)}", file=sys.stdout, flush=True)
            return

        chosen = options[idx - 1]
        value = chosen.get("value") if isinstance(chosen, dict) else str(chosen)
        question_id = pending["question_id"]
        self._pending_question = None

        handler = get_action_handler()
        if handler is None:
            log.warn("cli_action_handler_missing", question_id=question_id)
            return
        try:
            await handler(question_id, str(value), CLI_USER_ID)
        except Exception as e:
            log.error("cli_action_handler_failed", question_id=question_id, err=str(e))


    async def deliver(
        self,
        *,
        platform_id: str,
        thread_id: Optional[str],
        kind: str,
        content: dict,
        files: Optional[list[OutboundFile]] = None,
    ) -> Optional[str]:
        # ask_user_question 卡片 —— 打印标题 + 编号选项。
        if kind == "chat-sdk" and content.get("type") == "ask_question":
            question_id = content.get("question_id") or content.get("questionId")
            title = content.get("title") or ""
            question = content.get("question") or ""
            options = content.get("options") or []
            if not isinstance(options, list) or not options:
                print(f"[Agent] {title} (no options)", file=sys.stdout, flush=True)
                return None
            print(f"\n[Agent] {title}", file=sys.stdout, flush=True)
            if question:
                print(question, file=sys.stdout, flush=True)
            for i, opt in enumerate(options, start=1):
                label = opt.get("label") if isinstance(opt, dict) else str(opt)
                print(f"  {i}. {label}", file=sys.stdout, flush=True)
            print("(reply with a number)", file=sys.stdout, flush=True)
            self._pending_question = {
                "question_id": question_id,
                "options": options,
            }
            return None

        text = content.get("text") if isinstance(content, dict) else None
        if not text and isinstance(content, dict):
            text = json.dumps(content)
        prefix = "[Agent]"
        print(f"{prefix} {text or ''}", file=sys.stdout, flush=True)

        if files:
            for f in files:
                print(f"  (file: {f.filename}, {len(f.data)} bytes)", file=sys.stdout, flush=True)

        return None

    async def set_typing(self, platform_id: str, thread_id: Optional[str]) -> None:
        # no-op：stdout 没有 typing 指示器。不要打印任何东西
        # 每次刷新都打印会刷屏
        return None
