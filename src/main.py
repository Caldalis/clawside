from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from src.bootstrap import ensure_default_setup
from src.channels.adapter import (
    OutboundFile,
    get_adapter,
    set_action_handler,
    teardown_all_adapters,
)
from src.channels.adapter import init_all_adapters
from src.config import get_config
from src.db.connection import init_db, get_db, close_db
from src.db.migrations import run_migrations
from src.db.sessions import get_session
from src.delivery import (
    set_delivery_adapter,
    start_active_delivery_poll,
    start_sweep_delivery_poll,
    stop_delivery_polls,
)
from src.group_init import reconcile_all_groups
from src.sweep import start_host_sweep, stop_host_sweep
from src.log import log
from src.modules.permissions.access import install as install_permissions_hooks
from src.modules.scheduling import register_all as register_scheduling_actions
from src.session_manager import write_session_message
from src.cli.server import start_cli_server, stop_cli_server


def _make_delivery_adapter():

    class _Multiplex:
        async def deliver(
            self,
            channel_type: str,
            platform_id: str,
            thread_id,
            kind: str,
            content: str,
            files: Optional[list[OutboundFile]] = None,
        ):
            adapter = get_adapter(channel_type)
            if adapter is None:
                log.warn("delivery_no_adapter_for_channel", channel_type=channel_type)
                return None
            try:
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    parsed = {"text": str(parsed)}
            except (ValueError, TypeError):
                parsed = {"text": content or ""}
            return await adapter.deliver(
                platform_id=platform_id,
                thread_id=thread_id,
                kind=kind,
                content=parsed,
                files=files,
            )

        async def set_typing(self, channel_type, platform_id, thread_id) -> None:
            adapter = get_adapter(channel_type)
            if adapter is None:
                return
            try:
                await adapter.set_typing(platform_id, thread_id)
            except Exception:
                # 尽力而为。
                pass

    return _Multiplex()

def _resolve_choice_value(options_json: Optional[str], choice: str) -> str:
    try:
        idx = int(choice)
    except (TypeError, ValueError):
        return choice
    try:
        options = json.loads(options_json) if options_json else []
    except (TypeError, ValueError):
        return choice
    if not isinstance(options, list) or idx < 0 or idx >= len(options):
        return choice
    opt = options[idx]
    if isinstance(opt, dict):
        v = opt.get("value")
        return str(v) if v is not None else str(opt.get("label") or "")
    return str(opt)

async def _handle_question_response(
    question_id: str,
    choice: str,
    user_id: Optional[str],
) -> None:
    db = get_db()
    row = db.execute(
        "SELECT session_id, platform_id, channel_type, thread_id, message_out_id, options_json "
        "FROM pending_questions WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    if row is None:
        log.warn("action_unknown_question_id", question_id=question_id)
        return
    session_id = row["session_id"]
    sess = get_session(session_id)
    if sess is None:
        log.warn("action_session_missing", question_id=question_id, session_id=session_id)
        return

    value = _resolve_choice_value(row["options_json"], choice)

    msg_id = f"qresp-{question_id}"
    content = json.dumps({
        "question_id": question_id,
        "value": value,
        "user_id": user_id,
        "type": "question_response",
    })
    try:
        write_session_message(
            sess.agent_group_id,
            session_id,
            {
                "id": msg_id,
                "kind": "system",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "platform_id": row["platform_id"],
                "channel_type": row["channel_type"],
                "thread_id": row["thread_id"],
                "content": content,
                "trigger": 1,
            },
        )
    except Exception as e:
        log.error(
            "action_write_session_message_failed",
            question_id=question_id, session_id=session_id, err=str(e),
        )
        return

    try:
        db.execute(
            "DELETE FROM pending_questions WHERE question_id = ?",
            (question_id,),
        )
        db.commit()
    except Exception as e:
        log.warn(
            "action_cleanup_failed",
            question_id=question_id, err=str(e),
        )

    try:
        from src.container_runner import wake_container
        await wake_container(sess)
    except Exception as e:
        log.warn(
            "action_wake_failed",
            question_id=question_id, session_id=session_id, err=str(e),
        )


_long_running_tasks: list[asyncio.Task] = []


async def main() -> None:
    log.info("clawside_starting")

    cfg = get_config()
    db_path = os.path.join(cfg.data_dir_abs, "v2.db")
    db = init_db(db_path)
    run_migrations(db)
    log.info("central_db_ready", path=db_path)

    ensure_default_setup(db)
    reconcile_all_groups()

    register_scheduling_actions()
    install_permissions_hooks()
    set_action_handler(_handle_question_response)

    await init_all_adapters(cfg)

    set_delivery_adapter(_make_delivery_adapter())

    start_active_delivery_poll()
    start_sweep_delivery_poll()
    start_host_sweep()
    await start_cli_server()
    log.info("clawside_running")

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _request_shutdown(sig_name: str) -> None:
        log.info("shutdown_signal_received", signal=sig_name)

        loop.call_soon_threadsafe(shutdown_event.set)

    for s in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, s, None)
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, lambda s=s: _request_shutdown(s))
        except NotImplementedError:
            signal.signal(signum, lambda *_: _request_shutdown(s))

    try:
        await shutdown_event.wait()
    finally:
        await _shutdown()


async def _shutdown() -> None:
    log.info("clawside_shutting_down")
    try:
        await teardown_all_adapters()
    except Exception as e:
        log.warn("adapter_teardown_failed", err=str(e))
    try:
        stop_delivery_polls()
    except Exception as e:
        log.warn("stop_delivery_polls_failed", err=str(e))
    try:
        stop_host_sweep()
    except Exception as e:
        log.warn("stop_host_sweep_failed", err=str(e))
    try:
        await stop_cli_server()
    except Exception as e:
        log.warn("stop_cli_server_failed", err=str(e))
    try:
        close_db()
    except Exception as e:
        log.warn("close_db_failed", err=str(e))
    log.info("clawside_stopped")


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # 最后兜底
        sys.exit(0)
