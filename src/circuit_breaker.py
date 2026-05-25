from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional

from src.config import get_config
from src.log import log


WINDOW_SEC = 60
MAX_STARTS_IN_WINDOW = 3
BACKOFF_SEC = 30


def _startup_times_path() -> str:
    return os.path.join(get_config().data_dir_abs, ".startup_times")


def _load() -> dict[str, list[float]]:
    path = _startup_times_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[float]] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, list):
            continue
        out[k] = [float(t) for t in v if isinstance(t, (int, float))]
    return out


def _save(data: dict[str, list[float]]) -> None:
    path = _startup_times_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warn("circuit_breaker_save_failed", err=str(e))


def _prune(times: list[float], now: float) -> list[float]:
    cutoff = now - WINDOW_SEC
    return [t for t in times if t >= cutoff]


def record_startup(session_id: str) -> None:
    now = time.time()
    data = _load()
    times = _prune(data.get(session_id, []), now)
    times.append(now)
    data[session_id] = times
    _save(data)


def recent_startup_count(session_id: str, now: Optional[float] = None) -> int:
    cur = now if now is not None else time.time()
    data = _load()
    return len(_prune(data.get(session_id, []), cur))


async def wait_if_throttled(session_id: str) -> None:

    count = recent_startup_count(session_id)
    if count < MAX_STARTS_IN_WINDOW:
        return
    log.warn(
        "circuit_breaker_throttled",
        session_id=session_id,
        recent_starts=count,
        window_sec=WINDOW_SEC,
        sleeping_sec=BACKOFF_SEC,
    )
    await asyncio.sleep(BACKOFF_SEC)
