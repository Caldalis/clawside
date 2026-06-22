from __future__ import annotations

import asyncio
import dataclasses
import inspect
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.db.agent_groups import (
    AgentGroup, create_agent_group, delete_agent_group,
    get_agent_group, list_agent_groups, update_agent_group,
)
from src.db.connection import get_db
from src.db.messaging_groups import (
    MessagingGroup, MessagingGroupAgent,
    create_messaging_group, create_messaging_group_agent,
    delete_messaging_group, delete_messaging_group_agent,
    get_messaging_group, get_messaging_group_agents,
    list_messaging_groups, update_messaging_group,
)
from src.db.sessions import get_active_sessions, get_session
from src.db.users import get_user, list_users, upsert_user


class CliError(Exception):
    """以 {ok: false, error: {code, message}} 形式暴露到线协议。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _require(args: dict, name: str) -> Any:
    if name not in args or args[name] is None:
        raise CliError("missing-arg", f"missing required arg: {name}")
    return args[name]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj

def _groups_list(args: dict) -> Any:
    return [_to_dict(g) for g in list_agent_groups()]


def _groups_get(args: dict) -> Any:
    gid = _require(args, "id")
    g = get_agent_group(gid)
    if g is None:
        raise CliError("not-found", f"agent_group not found: {gid}")
    return _to_dict(g)


def _groups_create(args: dict) -> Any:
    gid = _require(args, "id")
    name = _require(args, "name")
    folder = _require(args, "folder")
    provider = args.get("agent_provider", "openai")
    g = AgentGroup(
        id=gid, name=name, folder=folder, agent_provider=provider,
        created_at=_now_iso(),
    )
    create_agent_group(g)

    try:
        from src.group_init import init_group_filesystem
        init_group_filesystem(g)
    except Exception:
        pass
    return _to_dict(g)


def _groups_update(args: dict) -> Any:
    gid = _require(args, "id")
    update_agent_group(
        gid,
        name=args.get("name"),
        agent_provider=args.get("agent_provider"),
    )
    return _to_dict(get_agent_group(gid))


def _groups_delete(args: dict) -> Any:
    gid = _require(args, "id")
    delete_agent_group(gid)
    return {"deleted": gid}


async def _groups_restart(args: dict) -> Any:

    gid = _require(args, "id")
    try:
        from src.container_runner import kill_container
    except ImportError:
        raise CliError("not-available", "container_runner not imported")
    killed: list[str] = []
    for s in get_active_sessions():
        if s.agent_group_id == gid:
            try:
                await kill_container(s.id, "ncl groups restart")
                killed.append(s.id)
            except Exception:
                pass
    return {"restarted": killed}



def _mg_list(args: dict) -> Any:
    return [_to_dict(m) for m in list_messaging_groups()]


def _mg_get(args: dict) -> Any:
    mid = _require(args, "id")
    m = get_messaging_group(mid)
    if m is None:
        raise CliError("not-found", f"messaging_group not found: {mid}")
    return _to_dict(m)


def _mg_create(args: dict) -> Any:
    mid = _require(args, "id")
    channel_type = _require(args, "channel_type")
    platform_id = _require(args, "platform_id")
    m = MessagingGroup(
        id=mid,
        channel_type=channel_type,
        platform_id=platform_id,
        name=args.get("name"),
        is_group=int(args.get("is_group", 0) or 0),
        unknown_sender_policy=args.get("unknown_sender_policy", "strict"),
        created_at=_now_iso(),
    )
    create_messaging_group(m)
    return _to_dict(m)


def _mg_update(args: dict) -> Any:
    mid = _require(args, "id")
    update_messaging_group(
        mid,
        name=args.get("name"),
        is_group=args.get("is_group"),
        unknown_sender_policy=args.get("unknown_sender_policy"),
    )
    return _to_dict(get_messaging_group(mid))


def _mg_delete(args: dict) -> Any:
    mid = _require(args, "id")
    delete_messaging_group(mid)
    return {"deleted": mid}



def _wirings_list(args: dict) -> Any:
    mgid = args.get("messaging_group_id")
    if mgid:
        return [_to_dict(w) for w in get_messaging_group_agents(mgid)]
    # 无过滤：枚举全部。
    rows = get_db().execute(
        "SELECT * FROM messaging_group_agents ORDER BY priority DESC, created_at"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "messaging_group_id": r["messaging_group_id"],
            "agent_group_id": r["agent_group_id"],
            "engage_mode": r["engage_mode"],
            "engage_pattern": r["engage_pattern"],
            "sender_scope": r["sender_scope"],
            "ignored_message_policy": r["ignored_message_policy"],
            "session_mode": r["session_mode"],
            "priority": r["priority"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _wirings_get(args: dict) -> Any:
    wid = _require(args, "id")
    row = get_db().execute(
        "SELECT * FROM messaging_group_agents WHERE id = ?", (wid,)
    ).fetchone()
    if row is None:
        raise CliError("not-found", f"wiring not found: {wid}")
    return {k: row[k] for k in row.keys()}


def _wirings_create(args: dict) -> Any:
    wid = _require(args, "id")
    w = MessagingGroupAgent(
        id=wid,
        messaging_group_id=_require(args, "messaging_group_id"),
        agent_group_id=_require(args, "agent_group_id"),
        engage_mode=args.get("engage_mode", "mention"),
        engage_pattern=args.get("engage_pattern"),
        sender_scope=args.get("sender_scope", "all"),
        ignored_message_policy=args.get("ignored_message_policy", "drop"),
        session_mode=args.get("session_mode", "shared"),
        priority=int(args.get("priority", 0) or 0),
        created_at=_now_iso(),
    )
    create_messaging_group_agent(w)
    return _to_dict(w)


def _wirings_update(args: dict) -> Any:
    wid = _require(args, "id")
    allowed = {
        "engage_mode", "engage_pattern", "sender_scope",
        "ignored_message_policy", "session_mode", "priority",
    }
    fields, values = [], {"id": wid}
    for k in list(args.keys()):
        if k in allowed and args[k] is not None:
            fields.append(f"{k} = :{k}")
            values[k] = args[k]
    if not fields:
        return _wirings_get({"id": wid})
    get_db().execute(
        f"UPDATE messaging_group_agents SET {', '.join(fields)} WHERE id = :id",
        values,
    )
    get_db().commit()
    return _wirings_get({"id": wid})


def _wirings_delete(args: dict) -> Any:
    wid = _require(args, "id")
    delete_messaging_group_agent(wid)
    return {"deleted": wid}




def _sessions_list(args: dict) -> Any:
    return [_to_dict(s) for s in get_active_sessions()]


def _sessions_get(args: dict) -> Any:
    sid = _require(args, "id")
    s = get_session(sid)
    if s is None:
        raise CliError("not-found", f"session not found: {sid}")
    return _to_dict(s)



def _users_list(args: dict) -> Any:
    return [_to_dict(u) for u in list_users()]


def _users_get(args: dict) -> Any:
    uid = _require(args, "id")
    u = get_user(uid)
    if u is None:
        raise CliError("not-found", f"user not found: {uid}")
    return _to_dict(u)


def _users_create(args: dict) -> Any:
    uid = _require(args, "id")
    kind = _require(args, "kind")
    upsert_user(uid, kind=kind, display_name=args.get("display_name"))
    return _to_dict(get_user(uid))



def _roles_list(args: dict) -> Any:
    rows = get_db().execute(
        "SELECT user_id, role, agent_group_id, granted_by, granted_at "
        "FROM user_roles ORDER BY agent_group_id, role, user_id"
    ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _roles_grant(args: dict) -> Any:
    user_id = _require(args, "user_id")
    role = _require(args, "role")
    agent_group_id = _require(args, "agent_group_id")
    if role not in ("owner", "admin"):
        raise CliError("bad-arg", f"role must be 'owner' or 'admin', got {role!r}")
    get_db().execute(
        """
        INSERT OR IGNORE INTO user_roles
          (user_id, role, agent_group_id, granted_by, granted_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, role, agent_group_id, args.get("granted_by"), _now_iso()),
    )
    get_db().commit()
    return {"granted": [user_id, role, agent_group_id]}


def _roles_revoke(args: dict) -> Any:
    user_id = _require(args, "user_id")
    role = _require(args, "role")
    agent_group_id = _require(args, "agent_group_id")
    cur = get_db().execute(
        "DELETE FROM user_roles "
        "WHERE user_id = ? AND role = ? AND agent_group_id = ?",
        (user_id, role, agent_group_id),
    )
    get_db().commit()
    return {"revoked": [user_id, role, agent_group_id], "rows": cur.rowcount}


Handler = Callable[[dict], Any]

_DISPATCH: dict[str, Handler] = {
    "groups.list":             _groups_list,
    "groups.get":              _groups_get,
    "groups.create":           _groups_create,
    "groups.update":           _groups_update,
    "groups.delete":           _groups_delete,
    "groups.restart":          _groups_restart,

    "messaging-groups.list":   _mg_list,
    "messaging-groups.get":    _mg_get,
    "messaging-groups.create": _mg_create,
    "messaging-groups.update": _mg_update,
    "messaging-groups.delete": _mg_delete,

    "wirings.list":            _wirings_list,
    "wirings.get":             _wirings_get,
    "wirings.create":          _wirings_create,
    "wirings.update":          _wirings_update,
    "wirings.delete":          _wirings_delete,

    "sessions.list":           _sessions_list,
    "sessions.get":            _sessions_get,

    "users.list":              _users_list,
    "users.get":               _users_get,
    "users.create":            _users_create,

    "roles.list":              _roles_list,
    "roles.grant":             _roles_grant,
    "roles.revoke":            _roles_revoke,
}


async def dispatch(frame: dict) -> dict:
    fid = frame.get("id", "unknown")
    command = frame.get("command", "")
    args = frame.get("args") or {}
    if not isinstance(args, dict):
        return {
            "id": fid, "ok": False,
            "error": {"code": "bad-args", "message": "args must be an object"},
        }
    handler = _DISPATCH.get(command)
    if handler is None:
        return {
            "id": fid, "ok": False,
            "error": {"code": "unknown-command", "message": f"unknown command: {command!r}"},
        }
    try:
        result = handler(args)
        if inspect.iscoroutine(result):
            result = await result
        return {"id": fid, "ok": True, "data": _to_dict(result)}
    except CliError as e:
        return {"id": fid, "ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception as e:
        return {
            "id": fid, "ok": False,
            "error": {"code": "internal-error", "message": str(e)},
        }
