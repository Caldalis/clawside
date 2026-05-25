from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv


_cfg: Optional["Config"] = None


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    openai_api_key: Optional[str]
    openai_base_url: str
    default_model: str

    container_image: str
    container_runtime: str           # 'docker'（目前唯一选项）


    telegram_bot_token: Optional[str]

    timezone: str
    groups_dir: str
    data_dir: str
    assistant_name: str
    cli_socket_path: str


    active_poll_ms: int = 1000
    sweep_poll_ms: int = 60_000
    absolute_ceiling_ms: int = 30 * 60 * 1000
    claim_stuck_ms: int = 60_000
    max_tries: int = 5
    backoff_base_ms: int = 5_000
    max_delivery_attempts: int = 3


    groups_dir_abs: str = field(default="", init=False)
    data_dir_abs: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.groups_dir_abs = os.path.abspath(self.groups_dir)
        self.data_dir_abs = os.path.abspath(self.data_dir)


def _load() -> Config:
    load_dotenv()
    return Config(
        openai_api_key=_env("OPENAI_API_KEY"),
        openai_base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1",
        default_model=_env("DEFAULT_MODEL", "gpt-4o") or "gpt-4o",
        container_image=_env("CONTAINER_IMAGE", "clawside-agent:latest") or "clawside-agent:latest",
        container_runtime=_env("CONTAINER_RUNTIME", "docker") or "docker",
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        timezone=_env("TIMEZONE", "UTC") or "UTC",
        groups_dir=_env("GROUPS_DIR", "./groups") or "./groups",
        data_dir=_env("DATA_DIR", "./data") or "./data",
        assistant_name=_env("ASSISTANT_NAME", "Andy") or "Andy",
        cli_socket_path=_env("CLI_SOCKET_PATH", "data/clawside.sock") or "data/clawside.sock",
        active_poll_ms=_env_int("ACTIVE_POLL_MS", 1000),
        sweep_poll_ms=_env_int("SWEEP_POLL_MS", 60_000),
        absolute_ceiling_ms=_env_int("ABSOLUTE_CEILING_MS", 30 * 60 * 1000),
        claim_stuck_ms=_env_int("CLAIM_STUCK_MS", 60_000),
        max_tries=_env_int("MAX_TRIES", 5),
        backoff_base_ms=_env_int("BACKOFF_BASE_MS", 5_000),
        max_delivery_attempts=_env_int("MAX_DELIVERY_ATTEMPTS", 3),
    )


def get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = _load()
    return _cfg


def reset_config_for_tests() -> None:
    """丢弃缓存的单例 修改环境变量的测试需调用此函数，
    以便下次 get_config() 看到新值。生产中不使用"""
    global _cfg
    _cfg = None
