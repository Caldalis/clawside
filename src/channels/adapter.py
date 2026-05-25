from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Config


@dataclass
class InboundMessage:
    id: str
    kind: str                  # 'chat' | 'task' | 'webhook' | 'system' | ...（消息种类）
    content: str               # JSON 字符串，携带 {text, sender, ...}
    timestamp: str
    is_mention: bool = False
    is_group: bool = False


@dataclass
class ReplyTo:

    channel_type: str
    platform_id: str
    thread_id: Optional[str] = None

@dataclass
class InboundEvent:
    channel_type: str
    platform_id: str
    thread_id: Optional[str]
    message: InboundMessage
    reply_to: Optional[ReplyTo] = None

@dataclass
class OutboundFile:
    filename: str
    data: bytes

InboundCallback = Callable[[InboundMessage], Awaitable[None]]
InboundEventCallback = Callable[[InboundEvent], Awaitable[None]]
MetadataCallback = Callable[[dict], Awaitable[None]]
ActionCallback = Callable[[dict], Awaitable[None]]


@dataclass
class ChannelSetup:
    on_inbound: Optional[InboundCallback] = None
    on_inbound_event: Optional[InboundEventCallback] = None
    on_metadata: Optional[MetadataCallback] = None
    on_action: Optional[ActionCallback] = None



class ChannelAdapter(ABC):


    channel_type: str = ""
    supports_threads: bool = False

    @abstractmethod
    async def deliver(
        self,
        *,
        platform_id: str,
        thread_id: Optional[str],
        kind: str,
        content: dict,
        files: Optional[list[OutboundFile]] = None,
    ) -> Optional[str]:
        """发送消息并返回平台的 message-id/ None"""

    async def set_typing(self, platform_id: str, thread_id: Optional[str]) -> None:
        """显示 typing 指示器。支持的适配器需重写"""
        return None

    async def subscribe(self, platform_id: str, thread_id: Optional[str]) -> None:
        """订阅一个 thread mention-sticky 跟随回复适配器按需重写"""
        return None

    async def shutdown(self) -> None:
        """拆卸适配器自有资源（轮询任务、网络客户端）。
        打开后台连接的适配器需重写。主机的 SIGTERM/SIGINT 路径会对每个
        已注册的适配器调用此方法。"""
        return None


_adapters: dict[str, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    if adapter.channel_type in _adapters:
        # 延迟导入，避免模块导入时 log<->adapter 循环。
        from src.log import log
        log.warn("adapter_overwrite", channel_type=adapter.channel_type)
    _adapters[adapter.channel_type] = adapter


def get_adapter(channel_type: str) -> Optional[ChannelAdapter]:
    return _adapters.get(channel_type)
def get_all_adapters() -> list[ChannelAdapter]:
    return list(_adapters.values())
def reset_adapters_for_tests() -> None:
    """清空所有已注册的适配器。生产中不使用。"""
    _adapters.clear()
    global _action_handler
    _action_handler = None



ActionHandlerFn = Callable[[str, str, Optional[str]], Awaitable[None]]
_action_handler: Optional[ActionHandlerFn] = None


def set_action_handler(fn: ActionHandlerFn) -> None:
    global _action_handler
    if _action_handler is not None:
        from src.log import log
        log.warn("action_handler_overwritten")
    _action_handler = fn


def get_action_handler() -> Optional[ActionHandlerFn]:
    return _action_handler


async def init_all_adapters(cfg: "Config") -> None:
    from src.log import log

    try:
        from src.channels.cli import CLIAdapter
        cli = CLIAdapter(cfg)
        register_adapter(cli)
        await cli.start()
    except ImportError:

        log.debug("cli_adapter_not_available")
    except Exception as e:
        log.error("cli_adapter_start_failed", err=str(e))

    if cfg.telegram_bot_token:
        try:
            from src.channels.telegram import TelegramAdapter
            tg = TelegramAdapter(cfg)
            register_adapter(tg)
            await tg.start()
        except ImportError:
            log.debug("telegram_adapter_not_available")
        except Exception as e:
            log.error("telegram_adapter_start_failed", err=str(e))


async def teardown_all_adapters() -> None:

    from src.log import log
    for a in list(_adapters.values()):
        try:
            await a.shutdown()
        except Exception as e:
            log.warn("adapter_shutdown_failed", channel_type=a.channel_type, err=str(e))
