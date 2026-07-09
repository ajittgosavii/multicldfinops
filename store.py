"""Durable state: scenarios, budget overrides, allocation policies, comments.

Streamlit Community Cloud's filesystem is ephemeral -- a local SQLite file does
not survive a reboot, and reboots happen on every push. So the store is
pluggable: set the `DATABASE_URL` secret to a Postgres instance (Neon, Supabase,
RDS) and state persists; leave it unset and we fall back to SQLite, which is
correct for local development and honest about its limits in the UI.

Nothing in the analytics path depends on this module. It stores decisions
(the shared-cost policy the CFO signed off, the budget Finance overrode), not
data.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

_LOCK = threading.Lock()
_SQLITE_PATH = os.environ.get("FINOPS_SQLITE_PATH", "finops_state.db")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS scenarios (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        payload     TEXT NOT NULL,
        created_by  TEXT,
        created_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budget_overrides (
        id          TEXT PRIMARY KEY,
        period      TEXT NOT NULL,
        cloud       TEXT,
        application TEXT,
        budget      DOUBLE PRECISION NOT NULL,
        note        TEXT,
        updated_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS policies (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        payload     TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 0,
        updated_at  TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id          TEXT PRIMARY KEY,
        target      TEXT NOT NULL,
        author      TEXT,
        body        TEXT NOT NULL,
        created_at  TIMESTAMP NOT NULL
    )
    """,
]


def _now() -> datetime:
    """Timezone-aware UTC. `datetime.utcnow()` is deprecated and returns a naive
    value, which SQLite and Postgres then interpret differently."""
    return datetime.now(timezone.utc)


def _database_url() -> Optional[str]:
    try:
        import streamlit as st

        if "DATABASE_URL" in st.secrets:
            return str(st.secrets["DATABASE_URL"])
    except Exception:
        pass
    return os.environ.get("DATABASE_URL")


@dataclass
class Backend:
    kind: str  # 'postgres' | 'sqlite'
    durable: bool
    detail: str


def backend() -> Backend:
    url = _database_url()
    if url:
        return Backend("postgres", True, "Durable Postgres store")
    return Backend(
        "sqlite",
        False,
        "Local SQLite. On Streamlit Cloud this is wiped on every reboot -- "
        "set the DATABASE_URL secret for durable state.",
    )


@contextmanager
def _conn() -> Iterator[Any]:
    url = _database_url()
    if url:
        try:
            import psycopg  # psycopg 3

            with psycopg.connect(url) as c:
                yield c
                return
        except ImportError:
            pass
    with _LOCK:
        c = sqlite3.connect(_SQLITE_PATH)
        try:
            yield c
            c.commit()
        finally:
            c.close()


def _placeholder() -> str:
    return "%s" if _database_url() else "?"


def init() -> None:
    """Idempotent. Safe to call on every Streamlit rerun."""
    with _conn() as c:
        cur = c.cursor()
        for stmt in SCHEMA:
            sql = stmt
            if not _database_url():
                sql = sql.replace("DOUBLE PRECISION", "REAL")
            cur.execute(sql)


def _exec(sql: str, params: tuple = ()) -> None:
    with _conn() as c:
        c.cursor().execute(sql, params)


def _query(sql: str, params: tuple = ()) -> List[tuple]:
    with _conn() as c:
        cur = c.cursor()
        cur.execute(sql, params)
        return cur.fetchall()


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def save_scenario(scenario_id: str, name: str, kind: str, payload: Dict[str, Any], created_by: str = "") -> None:
    p = _placeholder()
    _exec(
        f"INSERT INTO scenarios (id,name,kind,payload,created_by,created_at) VALUES ({p},{p},{p},{p},{p},{p})",
        (scenario_id, name, kind, json.dumps(payload), created_by, _now()),
    )


def list_scenarios(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    p = _placeholder()
    if kind:
        rows = _query(f"SELECT id,name,kind,payload,created_by,created_at FROM scenarios WHERE kind={p}", (kind,))
    else:
        rows = _query("SELECT id,name,kind,payload,created_by,created_at FROM scenarios")
    return [
        {"id": r[0], "name": r[1], "kind": r[2], "payload": json.loads(r[3]), "created_by": r[4], "created_at": r[5]}
        for r in rows
    ]


def delete_scenario(scenario_id: str) -> None:
    p = _placeholder()
    _exec(f"DELETE FROM scenarios WHERE id={p}", (scenario_id,))


# --------------------------------------------------------------------------
# Budget overrides
# --------------------------------------------------------------------------


def upsert_budget(row_id: str, period: str, cloud: str, application: str, budget: float, note: str = "") -> None:
    p = _placeholder()
    _exec(f"DELETE FROM budget_overrides WHERE id={p}", (row_id,))
    _exec(
        f"INSERT INTO budget_overrides (id,period,cloud,application,budget,note,updated_at) "
        f"VALUES ({p},{p},{p},{p},{p},{p},{p})",
        (row_id, period, cloud, application, float(budget), note, _now()),
    )


def list_budget_overrides() -> List[Dict[str, Any]]:
    rows = _query("SELECT id,period,cloud,application,budget,note,updated_at FROM budget_overrides")
    return [
        {"id": r[0], "period": r[1], "cloud": r[2], "application": r[3], "budget": r[4], "note": r[5], "updated_at": r[6]}
        for r in rows
    ]


# --------------------------------------------------------------------------
# Allocation policies
# --------------------------------------------------------------------------


def save_policy(policy_id: str, name: str, payload: Dict[str, Any], active: bool = False) -> None:
    p = _placeholder()
    _exec(f"DELETE FROM policies WHERE id={p}", (policy_id,))
    if active:
        _exec("UPDATE policies SET active=0", ())
    _exec(
        f"INSERT INTO policies (id,name,payload,active,updated_at) VALUES ({p},{p},{p},{p},{p})",
        (policy_id, name, json.dumps(payload), 1 if active else 0, _now()),
    )


def active_policy() -> Optional[Dict[str, Any]]:
    rows = _query("SELECT id,name,payload FROM policies WHERE active=1")
    if not rows:
        return None
    return {"id": rows[0][0], "name": rows[0][1], "payload": json.loads(rows[0][2])}


def list_policies() -> List[Dict[str, Any]]:
    rows = _query("SELECT id,name,payload,active FROM policies")
    return [{"id": r[0], "name": r[1], "payload": json.loads(r[2]), "active": bool(r[3])} for r in rows]


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------


def add_comment(comment_id: str, target: str, author: str, body: str) -> None:
    p = _placeholder()
    _exec(
        f"INSERT INTO comments (id,target,author,body,created_at) VALUES ({p},{p},{p},{p},{p})",
        (comment_id, target, author, body, _now()),
    )


def list_comments(target: str) -> List[Dict[str, Any]]:
    p = _placeholder()
    rows = _query(f"SELECT id,target,author,body,created_at FROM comments WHERE target={p}", (target,))
    return [{"id": r[0], "target": r[1], "author": r[2], "body": r[3], "created_at": r[4]} for r in rows]
