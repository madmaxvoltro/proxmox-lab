"""Human approvals for risky MCP actions (SPEC 16).

An approval is bound to one user and to the exact action: a SHA-256 over tool, project and
arguments. It is single-use and expires. The AI session can only create *pending* approvals;
only `lab approve` (a human at a host terminal) or an MCP elicitation answered in the client UI
can grant one."""

import hashlib
import json
import secrets
import time

from .db import DB
from .errors import AccessError, PreconditionError


class ApprovalRequired(AccessError):
    def __init__(self, approval_id: str, plan: str):
        self.approval_id, self.plan = approval_id, plan
        super().__init__(
            f"APPROVAL REQUIRED ({approval_id}): {plan}\n"
            f"Ask the user to run `lab approve {approval_id}` in a terminal on the host, then repeat "
            "exactly the same call. Do not attempt anything else to get around this.")


class ApprovalDenied(AccessError):
    pass


def fingerprint(tool: str, project: str | None, args: dict) -> str:
    blob = json.dumps({"tool": tool, "project": project, "args": args}, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Approvals:
    def __init__(self, db: DB, ttl: float, user: str, clock=time.time):
        self.db, self.ttl, self.user, self.clock = db, ttl, user, clock

    def _new_id(self) -> str:
        while True:
            i = secrets.token_hex(3)
            if self.db.approval_get(i) is None:
                return i

    def take(self, tool: str, project: str | None, args: dict):
        """Consume a matching human-granted approval, if any. Returns the row (for the audit trail)."""
        fp = fingerprint(tool, project, args)
        row = self.db.approval_find(self.user, fp, "approved", self.clock())
        if row and self.db.approval_consume(row["id"], self.clock()):
            return row
        return None

    def request(self, tool: str, project: str | None, args: dict, plan: str, session: str) -> str:
        """Create (or reuse) a pending approval and return its ID."""
        fp = fingerprint(tool, project, args)
        existing = self.db.approval_find(self.user, fp, "pending", self.clock())
        if existing:
            return existing["id"]
        aid = self._new_id()
        self.db.approval_create(aid, self.user, session, tool, project, fp, plan, self.clock(), self.ttl)
        return aid

    def record_granted(self, tool: str, project: str | None, args: dict, plan: str, session: str, by: str) -> str:
        """Trace of an approval answered through the client UI (already used)."""
        aid = self._new_id()
        self.db.approval_create(aid, self.user, session, tool, project, fingerprint(tool, project, args), plan,
                                self.clock(), self.ttl, status="used", decided_by=by)
        return aid

    # human side (`lab approve`) ------------------------------------------------------
    def decide(self, approval_id: str, approve: bool, decider_name: str, decider_is_admin: bool) -> None:
        row = self.db.approval_get(approval_id)
        if row is None or not (decider_is_admin or row["user"] == decider_name):
            raise PreconditionError(f"no such approval: {approval_id}")
        if not self.db.approval_decide(approval_id, "approved" if approve else "denied", decider_name, self.clock()):
            state = "expired" if row["status"] == "pending" else row["status"]
            raise PreconditionError(f"approval {approval_id} is {state}; it can no longer be decided")


def state_of(row, now: float) -> str:
    return "expired" if row["status"] in ("pending", "approved") and row["expires_at"] <= now else row["status"]
