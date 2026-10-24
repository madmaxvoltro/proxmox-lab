"""Caller identity and authorization (SPEC section 17)."""

import json
import os
from dataclasses import dataclass

from .db import DB
from .errors import AccessError, PreconditionError, UsageError
from .identity import caller_login
from .runner import Runner
from .units import validate_name


@dataclass(frozen=True)
class Caller:
    login: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _caller(row) -> Caller:
    return Caller(row["login"], row["name"], row["role"])


def resolve_caller(db: DB, env: dict, runner: Runner, *, as_name: str | None = None,
                   local_root: bool | None = None) -> Caller:
    """Who is running this command.

    Over SSH: the tailnet identity of the connecting client, which must be a lab user.
    On the host console as root (no SSH session): the first admin. `as_name` lets an admin
    act as another user (used by admin tooling; never grants more than that user has)."""
    if local_root is None:
        local_root = hasattr(os, "geteuid") and os.geteuid() == 0
    if "SSH_CONNECTION" in env:
        login = caller_login(env, runner)
        if not login:
            raise AccessError("cannot determine your tailnet identity; connect over Tailscale SSH")
        row = db.get_user(login)
        if row is None:
            raise AccessError(f"{login} is not a lab user; ask an admin to run `lab user add {login} <name>`")
        who = _caller(row)
    elif local_root:
        admins = [u for u in db.list_users() if u["role"] == "admin"]
        if not admins:
            raise AccessError("no admin exists yet; run `lab init`")
        who = _caller(admins[0])
    else:
        raise AccessError("run lab over SSH on the Proxmox host (as root on the console, or via Tailscale SSH)")
    if as_name and as_name != who.name:
        if not who.is_admin:
            raise AccessError("only admins can act as another user")
        row = db.get_user_by_name(as_name)
        if row is None:
            raise UsageError(f"no such lab user: {as_name}")
        return _caller(row)
    return who


def get_project(db: DB, caller: Caller, name: str):
    """Project row if the caller may see it. Someone else's project looks exactly like a missing one."""
    row = db.get_project(name)
    if row is None or not (caller.is_admin or row["owner"] == caller.name):
        raise PreconditionError(f"no such project: {name}")
    return row


def visible_projects(db: DB, caller: Caller, everything: bool = False):
    if caller.is_admin and everything:
        return db.list_projects()
    return db.list_projects(caller.name)


# user management (admin only) --------------------------------------------------------------
def require_admin(caller: Caller) -> None:
    if not caller.is_admin:
        raise AccessError("admin only")


def add_user(db: DB, caller: Caller, login: str, name: str, role: str = "user", quota: dict | None = None) -> None:
    require_admin(caller)
    validate_name(name)
    if db.get_user(login) or db.get_user_by_name(name):
        raise PreconditionError("that login or username already exists")
    db.add_user(login, name, role, quota)


def remove_user(db: DB, caller: Caller, name: str) -> None:
    require_admin(caller)
    row = db.get_user_by_name(name)
    if row is None:
        raise UsageError(f"no such lab user: {name}")
    if db.list_projects(name):
        raise PreconditionError(f"{name} still owns projects; delete or hand them over first")
    admins = [u for u in db.list_users() if u["role"] == "admin" and u["name"] != name]
    if row["role"] == "admin" and not admins:
        raise PreconditionError("cannot remove the last admin")
    db.remove_user(row["login"])


def user_quota_overrides(row) -> dict:
    return json.loads(row["quota_json"] or "{}")
