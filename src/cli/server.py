from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

from src.cli.commands import dispatch
from src.config import get_config
from src.log import log


_server: Optional[asyncio.AbstractServer] = None
_socket_path: Optional[str] = None



async def start_cli_server(socket_path: Optional[str] = None) -> None:

    global _server, _socket_path
    if _server is not None:
        return

    cfg = get_config()
    path = socket_path or cfg.cli_socket_path
    _socket_path = path

    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    if sys.platform == "win32":
        _server = await _start_tcp_fallback(path)
        return


    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        log.warn("cli_socket_unlink_failed", path=path, err=str(e))

    try:
        _server = await asyncio.start_unix_server(_handle_client, path=path)
    except (NotImplementedError, AttributeError, OSError) as e:
        # 针对异常 POSIX 环境的防御性回退
        log.warn("cli_unix_server_failed_falling_back", path=path, err=str(e))
        _server = await _start_tcp_fallback(path)
        return

    try:
        os.chmod(path, 0o600)
    except OSError as e:
        log.warn("cli_socket_chmod_failed", path=path, err=str(e))

    log.info("cli_server_listening", path=path, transport="unix")


async def stop_cli_server() -> None:

    global _server, _socket_path
    if _server is None:
        return
    s = _server
    _server = None
    s.close()
    try:
        await s.wait_closed()
    except Exception as e:
        log.warn("cli_server_wait_closed_failed", err=str(e))

    if _socket_path and sys.platform != "win32":
        try:
            if os.path.exists(_socket_path):
                os.unlink(_socket_path)
        except OSError as e:
            log.warn("cli_socket_cleanup_failed", path=_socket_path, err=str(e))
    _socket_path = None


async def _start_tcp_fallback(path: str) -> asyncio.AbstractServer:

    port = 49152 + (hash(os.path.abspath(path)) & 0x3FFF)
    server = await asyncio.start_server(_handle_client, host="127.0.0.1", port=port)
    log.info("cli_server_listening", path=path, transport="tcp", host="127.0.0.1", port=port)
    return server


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        line = await reader.readline()
    except Exception as e:
        log.warn("cli_read_failed", err=str(e))
        writer.close()
        return

    if not line:
        writer.close()
        return

    try:
        frame = json.loads(line.decode("utf-8").rstrip("\r\n"))
        if not isinstance(frame, dict):
            raise ValueError("frame must be a JSON object")
    except (ValueError, UnicodeDecodeError) as e:
        resp = {
            "id": "unknown", "ok": False,
            "error": {"code": "bad-frame", "message": f"bad request: {e}"},
        }
        await _write_frame(writer, resp)
        return

    resp = await dispatch(frame)
    await _write_frame(writer, resp)


async def _write_frame(writer: asyncio.StreamWriter, frame: dict) -> None:
    try:
        writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()
    except Exception as e:
        log.warn("cli_write_failed", err=str(e))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
