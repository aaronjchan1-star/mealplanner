"""
Database layer — raw sqlite3, no ORM, to keep the Pi Zero W happy.

Tables:
  preferences   — household-level settings (single row, key/value JSON blob)
  pantry        — ingredients on hand, with optional expiry
  dislikes      — cuisines / ingredients to avoid (per person if needed)
  plans         — generated meal plans (one row per plan, JSON payload)
  schedules     — recurring plan rules
  feedback      — thumbs up/down on meals so the AI learns what works
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pantry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    quantity TEXT,
    expires_on TEXT,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dislikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'household',
    item TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'ingredient',  -- 'ingredient' | 'cuisine' | 'allergy'
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    audience TEXT NOT NULL,           -- 'family' | 'toddler'
    payload TEXT NOT NULL,
    budget_cents INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audience TEXT NOT NULL,
    cadence TEXT NOT NULL,            -- 'weekly' | 'fortnightly'
    next_run TEXT NOT NULL,
    params TEXT NOT NULL,             -- JSON: budget, max_cook_time etc.
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER,
    meal_name TEXT NOT NULL,
    rating INTEGER NOT NULL,          -- -1, 0, +1
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE SET NULL
);
"""


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str, seed_household: Dict[str, Any]) -> None:
    with get_conn(db_path) as c:
        c.executescript(SCHEMA)
        row = c.execute("SELECT 1 FROM preferences WHERE id = 1").fetchone()
        if not row:
            c.execute(
                "INSERT INTO preferences (id, payload, updated_at) VALUES (1, ?, ?)",
                (json.dumps(seed_household), _now()),
            )


# --- Preferences ----------------------------------------------------------

def get_preferences(db_path: str) -> Dict[str, Any]:
    with get_conn(db_path) as c:
        row = c.execute("SELECT payload FROM preferences WHERE id = 1").fetchone()
        return json.loads(row["payload"]) if row else {}


def set_preferences(db_path: str, payload: Dict[str, Any]) -> None:
    with get_conn(db_path) as c:
        c.execute(
            "UPDATE preferences SET payload = ?, updated_at = ? WHERE id = 1",
            (json.dumps(payload), _now()),
        )


# --- Pantry ---------------------------------------------------------------

def list_pantry(db_path: str) -> List[Dict[str, Any]]:
    with get_conn(db_path) as c:
        rows = c.execute(
            "SELECT id, name, quantity, expires_on, added_at FROM pantry ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]


def add_pantry_item(db_path: str, name: str, quantity: Optional[str] = None,
                    expires_on: Optional[str] = None) -> int:
    with get_conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO pantry (name, quantity, expires_on, added_at) VALUES (?, ?, ?, ?)",
            (name.strip(), (quantity or "").strip() or None, expires_on, _now()),
        )
        return cur.lastrowid


def remove_pantry_item(db_path: str, item_id: int) -> None:
    with get_conn(db_path) as c:
        c.execute("DELETE FROM pantry WHERE id = ?", (item_id,))


# --- Dislikes -------------------------------------------------------------

def list_dislikes(db_path: str) -> List[Dict[str, Any]]:
    with get_conn(db_path) as c:
        rows = c.execute(
            "SELECT id, person, item, kind FROM dislikes ORDER BY kind, item"
        ).fetchall()
        return [dict(r) for r in rows]


def add_dislike(db_path: str, item: str, kind: str = "ingredient",
                person: str = "household") -> int:
    with get_conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO dislikes (person, item, kind, added_at) VALUES (?, ?, ?, ?)",
            (person, item.strip(), kind, _now()),
        )
        return cur.lastrowid


def remove_dislike(db_path: str, dislike_id: int) -> None:
    with get_conn(db_path) as c:
        c.execute("DELETE FROM dislikes WHERE id = ?", (dislike_id,))


# --- Plans ----------------------------------------------------------------

def save_plan(db_path: str, week_start: str, audience: str,
              payload: Dict[str, Any], budget_cents: Optional[int]) -> int:
    with get_conn(db_path) as c:
        cur = c.execute(
            """INSERT INTO plans (week_start, audience, payload, budget_cents, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (week_start, audience, json.dumps(payload), budget_cents, _now()),
        )
        return cur.lastrowid


def list_plans(db_path: str, audience: Optional[str] = None,
               limit: int = 20) -> List[Dict[str, Any]]:
    with get_conn(db_path) as c:
        if audience:
            rows = c.execute(
                """SELECT id, week_start, audience, payload, budget_cents, created_at
                   FROM plans WHERE audience = ? ORDER BY created_at DESC LIMIT ?""",
                (audience, limit),
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT id, week_start, audience, payload, budget_cents, created_at
                   FROM plans ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out


def get_plan(db_path: str, plan_id: int) -> Optional[Dict[str, Any]]:
    with get_conn(db_path) as c:
        row = c.execute(
            """SELECT id, week_start, audience, payload, budget_cents, created_at
               FROM plans WHERE id = ?""",
            (plan_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d


def delete_plan(db_path: str, plan_id: int) -> None:
    """Remove a plan and any feedback rows pointing at it."""
    with get_conn(db_path) as c:
        c.execute("DELETE FROM plans WHERE id = ?", (plan_id,))


# --- Schedules ------------------------------------------------------------

def list_schedules(db_path: str) -> List[Dict[str, Any]]:
    with get_conn(db_path) as c:
        rows = c.execute(
            "SELECT id, audience, cadence, next_run, params, active FROM schedules"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"])
            out.append(d)
        return out


def add_schedule(db_path: str, audience: str, cadence: str, next_run: str,
                 params: Dict[str, Any]) -> int:
    with get_conn(db_path) as c:
        cur = c.execute(
            """INSERT INTO schedules (audience, cadence, next_run, params, active)
               VALUES (?, ?, ?, ?, 1)""",
            (audience, cadence, next_run, json.dumps(params)),
        )
        return cur.lastrowid


def update_schedule_next_run(db_path: str, schedule_id: int, next_run: str) -> None:
    with get_conn(db_path) as c:
        c.execute(
            "UPDATE schedules SET next_run = ? WHERE id = ?",
            (next_run, schedule_id),
        )


def toggle_schedule(db_path: str, schedule_id: int, active: bool) -> None:
    with get_conn(db_path) as c:
        c.execute(
            "UPDATE schedules SET active = ? WHERE id = ?",
            (1 if active else 0, schedule_id),
        )


def delete_schedule(db_path: str, schedule_id: int) -> None:
    with get_conn(db_path) as c:
        c.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


# --- Feedback -------------------------------------------------------------

def record_feedback(db_path: str, plan_id: Optional[int], meal_name: str,
                    rating: int, note: Optional[str] = None) -> None:
    with get_conn(db_path) as c:
        c.execute(
            """INSERT INTO feedback (plan_id, meal_name, rating, note, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (plan_id, meal_name, rating, note, _now()),
        )


def recent_feedback(db_path: str, limit: int = 30) -> List[Dict[str, Any]]:
    with get_conn(db_path) as c:
        rows = c.execute(
            """SELECT meal_name, rating, note, created_at FROM feedback
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# --- helpers --------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
