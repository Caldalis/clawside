from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from src.circuit_breaker import record_startup, wait_if_throttled
from src.config import get_config
from src.container_config import ContainerConfig, materialize_container_json
from src.db.agent_destinations import (
    list_destinations_for_group,
    to_inbound_destination_rows,
)
from src.db.agent_groups import AgentGroup, get_agent_group
from src.db.session_db import (
    open_inbound_db as _open_inbound_raw,
    replace_destinations,
    upsert_session_routing,
)
from src.db.messaging_groups import get_messaging_group
from src.db.sessions import Session, get_session
from src.log import log
from src.session_manager import (
    heartbeat_path,
    inbound_db_path,
    mark_container_running,
    mark_container_stopped,
    session_dir,
)


@dataclass
class _Active:
    process: asyncio.subprocess.Process
    container_name: str
    watcher: asyncio.Task


_active: dict[str, _Active] = {}
_wake_futures: dict[str, asyncio.Future[bool]] = {}

_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


@dataclass
class VolumeMount:
    host_path: str
    container_path: str
    readonly: bool = False


def is_container_running(session_id: str) -> bool:
    return session_id in _active


def get_active_container_count() -> int:
    return len(_active)


async def wake_container(session: Session) -> bool:

    if session.id in _active:
        log.debug("container_already_running", session_id=session.id)
        return True

    existing = _wake_futures.get(session.id)
    if existing is not None:
        log.debug("container_wake_in_flight_join", session_id=session.id)
        return await existing

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[bool] = loop.create_future()
    _wake_futures[session.id] = fut

    try:
        await _spawn_container(session)
        fut.set_result(True)
        return True
    except Exception as e:
        log.warn(
            "container_wake_failed_will_retry",
            session_id=session.id, err=str(e),
        )
        fut.set_result(False)
        return False
    finally:
        _wake_futures.pop(session.id, None)


async def kill_container(
    session_id: str,
    reason: str,
    on_exit: Optional[Callable[[], None | Awaitable[None]]] = None,
) -> None:
    entry = _active.get(session_id)
    if entry is None:
        if on_exit is not None:

            loop = _get_loop()
            if loop is not None:
                loop.create_task(_invoke(on_exit))
        return

    log.info(
        "container_killing",
        session_id=session_id, reason=reason, container_name=entry.container_name,
    )

    if on_exit is not None:
        entry.watcher.add_done_callback(lambda _t: _schedule(on_exit))

    if not await _stop_container(entry.container_name):
        with contextlib.suppress(ProcessLookupError):
            entry.process.kill()


async def _spawn_container(session: Session) -> None:
    cfg = get_config()
    agent_group = get_agent_group(session.agent_group_id)
    if agent_group is None:
        log.error(
            "container_spawn_no_agent_group",
            session_id=session.id, agent_group_id=session.agent_group_id,
        )
        raise RuntimeError(f"agent group not found: {session.agent_group_id}")

    _refresh_session_routing_and_destinations(session)

    container_config = materialize_container_json(agent_group.id)

    mounts = _build_mounts(agent_group, session, container_config)
    container_name = f"clawside-{agent_group.folder}-{int(time.time() * 1000)}"
    args = _build_container_args(mounts, container_name, container_config)

    log.info(
        "container_spawning",
        session_id=session.id,
        agent_group=agent_group.name,
        container_name=container_name,
        image=container_config.image_tag,
    )

    hb = heartbeat_path(agent_group.id, session.id)
    with contextlib.suppress(FileNotFoundError, OSError):
        os.unlink(hb)

    await wait_if_throttled(session.id)
    record_startup(session.id)

    runtime_bin = shutil.which(cfg.container_runtime) or cfg.container_runtime
    proc = await asyncio.create_subprocess_exec(
        runtime_bin, *args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    watcher = asyncio.create_task(
        _watch_container(session.id, proc, container_name, agent_group.folder),
        name=f"watch:{container_name}",
    )

    _active[session.id] = _Active(
        process=proc, container_name=container_name, watcher=watcher,
    )
    mark_container_running(session.id)


def _refresh_session_routing_and_destinations(session: Session) -> None:

    db_path = inbound_db_path(session.agent_group_id, session.id)
    if not os.path.exists(db_path):

        log.warn(
            "container_spawn_no_inbound_db",
            session_id=session.id, path=db_path,
        )
        return

    channel_type: Optional[str] = None
    platform_id: Optional[str] = None
    if session.messaging_group_id:
        mg = get_messaging_group(session.messaging_group_id)
        if mg is not None:
            channel_type = mg.channel_type
            platform_id = mg.platform_id

    dest_rows = to_inbound_destination_rows(
        list_destinations_for_group(session.agent_group_id)
    )

    db = _open_inbound_raw(db_path)
    try:
        upsert_session_routing(
            db,
            channel_type=channel_type,
            platform_id=platform_id,
            thread_id=session.thread_id,
        )
        replace_destinations(db, dest_rows)
    finally:
        db.close()


def _build_mounts(
    agent_group: AgentGroup,
    session: Session,
    container_config: ContainerConfig,
) -> list[VolumeMount]:
    cfg = get_config()
    mounts: list[VolumeMount] = []

    sess_dir = session_dir(agent_group.id, session.id)
    group_dir = os.path.join(cfg.groups_dir_abs, agent_group.folder)

    mounts.append(VolumeMount(host_path=sess_dir, container_path="/workspace"))

    mounts.append(VolumeMount(host_path=group_dir, container_path="/workspace/agent"))

    container_json = os.path.join(group_dir, "container.json")
    if os.path.exists(container_json):
        mounts.append(VolumeMount(
            host_path=container_json,
            container_path="/workspace/agent/container.json",
            readonly=True,
        ))

    global_dir = os.path.join(cfg.groups_dir_abs, "global")
    if os.path.isdir(global_dir):
        mounts.append(VolumeMount(
            host_path=global_dir,
            container_path="/workspace/global",
            readonly=True,
        ))


    for raw in container_config.additional_mounts:
        if not isinstance(raw, dict):
            continue
        host_path = raw.get("host_path")
        container_path = raw.get("container_path")
        if not isinstance(host_path, str) or not isinstance(container_path, str):
            continue
        if not host_path or not container_path:
            continue
        mounts.append(VolumeMount(
            host_path=host_path,
            container_path=container_path,
            readonly=bool(raw.get("readonly", False)),
        ))
    return mounts


def _build_container_args(
    mounts: list[VolumeMount],
    container_name: str,
    container_config: ContainerConfig,
) -> list[str]:
    cfg = get_config()
    if not _CONTAINER_NAME_RE.match(container_name):
        raise ValueError(f"invalid container name: {container_name!r}")

    args: list[str] = ["run", "--rm", "--name", container_name]

    args += ["-e", f"TZ={cfg.timezone}"]
    if cfg.openai_api_key:
        args += ["-e", f"OPENAI_API_KEY={cfg.openai_api_key}"]
    args += ["-e", f"OPENAI_BASE_URL={cfg.openai_base_url}"]
    args += ["-e", f"DEFAULT_MODEL={container_config.model}"]
    args += ["-e", f"ASSISTANT_NAME={container_config.assistant_name}"]

    for m in mounts:
        spec = f"{m.host_path}:{m.container_path}"
        if m.readonly:
            spec += ":ro"
        args += ["-v", spec]

    args.append(container_config.image_tag)

    args += ["python", "-m", "agent_runner.main"]

    return args


async def _watch_container(
    session_id: str,
    proc: asyncio.subprocess.Process,
    container_name: str,
    group_folder: str,
) -> None:
    stderr_task: Optional[asyncio.Task] = None
    if proc.stderr is not None:
        stderr_task = asyncio.create_task(_pipe_to_log(proc.stderr, group_folder))
    stdout_task: Optional[asyncio.Task] = None
    if proc.stdout is not None:
        stdout_task = asyncio.create_task(_drain(proc.stdout))

    try:
        rc = await proc.wait()
    except Exception as e:
        log.error("container_wait_failed", session_id=session_id, err=str(e))
        rc = -1
    finally:
        if stderr_task is not None:
            with contextlib.suppress(Exception):
                await stderr_task
        if stdout_task is not None:
            with contextlib.suppress(Exception):
                await stdout_task

    _active.pop(session_id, None)
    mark_container_stopped(session_id)
    log.info(
        "container_exited",
        session_id=session_id, code=rc, container_name=container_name,
    )


async def _pipe_to_log(stream: asyncio.StreamReader, group_folder: str) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            log.debug("container_stderr", container=group_folder, line=text)


async def _drain(stream: asyncio.StreamReader) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return

async def _stop_container(name: str) -> bool:
    if not _CONTAINER_NAME_RE.match(name):
        log.warn("container_stop_invalid_name", name=name)
        return False
    cfg = get_config()
    runtime_bin = shutil.which(cfg.container_runtime) or cfg.container_runtime
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime_bin, "stop", "-t", "1", name,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError) as e:
        log.warn("container_stop_failed", name=name, err=str(e))
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        log.warn("container_stop_failed", name=name, err="timeout")
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False
    return proc.returncode == 0


def _get_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _schedule(cb: Callable[[], None | Awaitable[None]]) -> None:
    loop = _get_loop()
    if loop is None:
        return
    loop.create_task(_invoke(cb))


async def _invoke(cb: Callable[[], None | Awaitable[None]]) -> None:
    try:
        result = cb()
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        log.error("container_on_exit_callback_failed", err=str(e))
