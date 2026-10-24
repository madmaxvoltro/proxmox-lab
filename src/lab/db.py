"""SQLite index (SPEC section 3). Proxmox is the source of truth; this is an index plus a journal."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import LabError

STATES = (
    "creating", "running", "stopped", "archiving", "archived",
    "restoring", "deleting", "failed",
)
ROLES = ("admin", "user")

_MIGRATIONS = [
    # v1
    """
    CREATE TABLE users (
        login      TEXT PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        role       TEXT NOT NULL CHECK (role IN ('admin','user')),
        quota_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );
    CREATE TABLE projects (
        name         TEXT PRIMARY KEY,
        vmid         INTEGER UNIQUE,
        owner        TEXT NOT NULL REFERENCES users(name),
        distro       TEXT NOT NULL,
        template_id  INTEGER,
        storage_id   TEXT,
        clone_mode   TEXT NOT NULL DEFAULT 'full',
        cpu          INTEGER NOT NULL,
        ram_mb       INTEGER NOT NULL,
        disk_gb      INTEGER NOT NULL,
        state        TEXT NOT NULL,
        protected    INTEGER NOT NULL DEFAULT 0,
        tailnet_name TEXT,
        tailnet_ip   TEXT,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL,
        last_active  REAL
    );
    CREATE TABLE archives (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project     TEXT NOT NULL,
        owner       TEXT NOT NULL,
        volid       TEXT NOT NULL,
        size_bytes  INTEGER,
        sha256      TEXT,
        created_at  REAL NOT NULL,
        verified_at REAL
    );
    CREATE INDEX archives_project ON archives(project);
    CREATE TABLE journal (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        project     TEXT NOT NULL,
        op          TEXT NOT NULL,
        step        TEXT,
        status      TEXT NOT NULL CHECK (status IN ('running','ok','failed','rolled_back')),
        detail      TEXT,
        actor       TEXT,
        started_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    );
    CREATE INDEX journal_open ON journal(status);
    """,
    # v2: MCP approvals
    """
    CREATE TABLE approvals (
        id          TEXT PRIMARY KEY,
        user        TEXT NOT NULL,
        session     TEXT NOT NULL,
        tool        TEXT NOT NULL,
        project     TEXT,
        fingerprint TEXT NOT NULL,
        plan        TEXT NOT NULL,
        status      TEXT NOT NULL CHECK (status IN ('pending','approved','denied','used')),
        created_at  REAL NOT NULL,
        expires_at  REAL NOT NULL,
        decided_by  TEXT,
        decided_at  REAL,
        used_at     REAL
    );
    CREATE INDEX approvals_fp ON approvals(user, fingerprint);
    """,
    # v3: static addresses (NULL when the network uses DHCP; unique index ignores NULLs)
    """
    ALTER TABLE projects ADD COLUMN static_ip TEXT;
    CREATE UNIQUE INDEX projects_static_ip ON projects(static_ip);
    """,
    # v4: liveness of the desktop approval helper (`lab watch`), so the MCP server knows whether to wait for a click
    """
    CREATE TABLE heartbeat (
        name    TEXT NOT NULL,
        user    TEXT NOT NULL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (name, user)
    );
    """,
]


class DBError(LabError):
    pass


def _now() -> float:
    return time.time()


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # we manage transactions
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if current > len(_MIGRATIONS):
            raise DBError(f"database schema v{current} is newer than this lab (v{len(_MIGRATIONS)})")
        for version, script in enumerate(_MIGRATIONS[current:], start=current + 1):
            with self.tx():
                for stmt in filter(None, (s.strip() for s in script.split(";"))):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version = {version}")

    @contextmanager
    def tx(self):
        """Explicit transaction (the connection is in autocommit mode); nested use joins the outer one."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # users -----------------------------------------------------------------
    def add_user(self, login: str, name: str, role: str = "user", quota: dict | None = None) -> None:
        if role not in ROLES:
            raise DBError(f"invalid role {role!r}")
        with self.tx():
            self.conn.execute(
                "INSERT INTO users(login,name,role,quota_json,created_at) VALUES (?,?,?,?,?)",
                (login, name, role, json.dumps(quota or {}), _now()),
            )

    def get_user(self, login: str):
        return self.conn.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()

    def get_user_by_name(self, name: str):
        return self.conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()

    def list_users(self):
        return self.conn.execute("SELECT * FROM users ORDER BY name").fetchall()

    def remove_user(self, login: str) -> None:
        with self.tx():
            self.conn.execute("DELETE FROM users WHERE login=?", (login,))

    # projects --------------------------------------------------------------
    def add_project(self, **f) -> None:
        if f.get("state", "creating") not in STATES:
            raise DBError(f"invalid state {f.get('state')!r}")
        now = _now()
        f.setdefault("state", "creating")
        f.setdefault("created_at", now)
        f["updated_at"] = now
        cols = ",".join(f)
        marks = ",".join("?" for _ in f)
        with self.tx():
            self.conn.execute(f"INSERT INTO projects({cols}) VALUES ({marks})", tuple(f.values()))

    def get_project(self, name: str):
        return self.conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()

    def list_projects(self, owner: str | None = None):
        if owner is None:
            return self.conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return self.conn.execute("SELECT * FROM projects WHERE owner=? ORDER BY name", (owner,)).fetchall()

    def update_project(self, name: str, **f) -> None:
        if "state" in f and f["state"] not in STATES:
            raise DBError(f"invalid state {f['state']!r}")
        f["updated_at"] = _now()
        sets = ",".join(f"{k}=?" for k in f)
        with self.tx():
            cur = self.conn.execute(f"UPDATE projects SET {sets} WHERE name=?", (*f.values(), name))
            if cur.rowcount == 0:
                raise DBError(f"no such project: {name}")

    def delete_project(self, name: str) -> None:
        with self.tx():
            self.conn.execute("DELETE FROM projects WHERE name=?", (name,))

    def used_vmids(self) -> set:
        return {r[0] for r in self.conn.execute("SELECT vmid FROM projects WHERE vmid IS NOT NULL")}

    def used_ips(self) -> set:
        return {r[0] for r in self.conn.execute("SELECT static_ip FROM projects WHERE static_ip IS NOT NULL")}

    # archives --------------------------------------------------------------
    def add_archive(self, project: str, owner: str, volid: str, size_bytes: int | None = None,
                    sha256: str | None = None, verified: bool = False) -> int:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO archives(project,owner,volid,size_bytes,sha256,created_at,verified_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (project, owner, volid, size_bytes, sha256, _now(), _now() if verified else None),
            )
            return cur.lastrowid

    def list_archives(self, project: str | None = None):
        if project is None:
            return self.conn.execute("SELECT * FROM archives ORDER BY created_at").fetchall()
        return self.conn.execute(
            "SELECT * FROM archives WHERE project=? ORDER BY created_at", (project,)
        ).fetchall()

    def delete_archive(self, archive_id: int) -> None:
        with self.tx():
            self.conn.execute("DELETE FROM archives WHERE id=?", (archive_id,))

    # journal ---------------------------------------------------------------
    def journal_begin(self, project: str, op: str, actor: str | None = None) -> int:
        now = _now()
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO journal(project,op,status,actor,started_at,updated_at)"
                " VALUES (?,?, 'running', ?,?,?)",
                (project, op, actor, now, now),
            )
            return cur.lastrowid

    def journal_step(self, jid: int, step: str, detail: str | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE journal SET step=?, detail=?, updated_at=? WHERE id=?",
                (step, detail, _now(), jid),
            )

    def journal_finish(self, jid: int, status: str = "ok", detail: str | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE journal SET status=?, detail=COALESCE(?, detail), updated_at=? WHERE id=?",
                (status, detail, _now(), jid),
            )

    def list_journal(self, project: str, limit: int = 50):
        return self.conn.execute(
            "SELECT * FROM journal WHERE project=? ORDER BY id DESC LIMIT ?", (project, limit)).fetchall()

    # approvals -------------------------------------------------------------
    def approval_create(self, id: str, user: str, session: str, tool: str, project: str | None,
                        fingerprint: str, plan: str, now: float, ttl: float, status: str = "pending",
                        decided_by: str | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO approvals(id,user,session,tool,project,fingerprint,plan,status,created_at,"
                "expires_at,decided_by,decided_at,used_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id, user, session, tool, project, fingerprint, plan, status, now, now + ttl, decided_by,
                 now if decided_by else None, now if status == "used" else None))

    def heartbeat(self, name: str, user: str, now: float) -> None:
        with self.tx():
            self.conn.execute("INSERT INTO heartbeat(name,user,seen_at) VALUES (?,?,?) "
                              "ON CONFLICT(name,user) DO UPDATE SET seen_at=excluded.seen_at", (name, user, now))

    def heartbeat_age(self, name: str, user: str, now: float) -> float | None:
        row = self.conn.execute("SELECT seen_at FROM heartbeat WHERE name=? AND user=?", (name, user)).fetchone()
        return None if row is None else now - row[0]

    def approval_get(self, id: str):
        return self.conn.execute("SELECT * FROM approvals WHERE id=?", (id,)).fetchone()

    def approval_find(self, user: str, fingerprint: str, status: str, now: float):
        return self.conn.execute(
            "SELECT * FROM approvals WHERE user=? AND fingerprint=? AND status=? AND expires_at>? "
            "ORDER BY created_at DESC LIMIT 1", (user, fingerprint, status, now)).fetchone()

    def approval_list(self, user: str | None = None, limit: int = 50):
        if user is None:
            return self.conn.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return self.conn.execute("SELECT * FROM approvals WHERE user=? ORDER BY created_at DESC LIMIT ?",
                                 (user, limit)).fetchall()

    def approval_decide(self, id: str, status: str, by: str, now: float) -> bool:
        """Approve or deny a pending, unexpired approval. Returns whether it changed."""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? "
                "WHERE id=? AND status='pending' AND expires_at>?", (status, by, now, id, now))
            return cur.rowcount == 1

    def approval_consume(self, id: str, now: float) -> bool:
        """Single use: only one caller can flip approved -> used."""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE approvals SET status='used', used_at=? WHERE id=? AND status='approved' AND expires_at>?",
                (now, id, now))
            return cur.rowcount == 1

    def get_journal(self, jid: int):
        return self.conn.execute("SELECT * FROM journal WHERE id=?", (jid,)).fetchone()

    def open_journal(self):
        """Operations still marked running: candidates for `lab doctor` resume/rollback."""
        return self.conn.execute("SELECT * FROM journal WHERE status='running' ORDER BY id").fetchall()
