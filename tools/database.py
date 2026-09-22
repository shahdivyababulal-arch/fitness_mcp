"""SQLite schema, connection helpers, and CRUD for biometrics logs."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from config import settings

# How close together two byte-identical entries have to be before the second
# is treated as a repeat of the first rather than a real second helping.
#
# A duplicated A2A delegation replays log_entry seconds apart -- the one
# observed was 21s. Someone genuinely eating the same thing twice does it
# minutes or hours apart, so a two-minute window separates the two cases
# without a client-supplied idempotency key, which the MCP tool signature
# has no room for.
DEDUPE_WINDOW_SECONDS = 120

# Configurable so a deployment can point it at a mounted volume; the default
# sits beside this module. On Cloud Run it lands on the container's writable
# layer, which means per-instance and lost on restart.
DB_PATH = Path(settings.db_path)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    """Initializes SQLite schema if not already present."""
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS food_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                item_name TEXT NOT NULL,
                serving_g REAL NOT NULL,
                calories REAL NOT NULL,
                protein_g REAL DEFAULT 0.0,
                carbs_g REAL DEFAULT 0.0,
                fat_g REAL DEFAULT 0.0,
                -- `timestamp` is a date, which is all the daily summary
                -- needs but too coarse to tell a replay from a second
                -- helping. This carries the full instant.
                created_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                activity TEXT NOT NULL,
                duration_min INTEGER NOT NULL,
                calories_burned REAL NOT NULL,
                created_at TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_targets (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                target_daily_calories REAL NOT NULL,
                target_protein_g REAL NOT NULL,
                target_carbs_g REAL NOT NULL,
                target_fat_g REAL NOT NULL,
                goal TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _ensure_created_at(conn: sqlite3.Connection) -> None:
    """Add created_at to databases written before it existed.

    Cloud Run's SQLite is per-instance and ephemeral, so in practice every
    database is fresh -- but a local file survives across runs, and an
    upgrade that silently stopped deduplicating would be worse than a
    migration that runs and does nothing.
    """
    for table in ("food_logs", "workout_logs"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "created_at" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN created_at TEXT")


def _recent_duplicate(
    conn: sqlite3.Connection, table: str, where: str, params: tuple
) -> Optional[int]:
    """The id of an identical row written inside the dedupe window, if any.

    Rows predating the created_at column have it NULL and never match, so
    an old database degrades to the previous behaviour rather than
    collapsing a day's history into one entry.
    """
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=DEDUPE_WINDOW_SECONDS)
    ).isoformat()
    row = conn.execute(
        f"SELECT id FROM {table} WHERE {where} AND created_at IS NOT NULL "
        f"AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (*params, cutoff),
    ).fetchone()
    return int(row[0]) if row else None


def insert_food(
    name: str,
    serving_g: float,
    calories: float,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
) -> tuple[int, bool]:
    """Log a meal. Returns (row id, whether this repeated a recent entry).

    Idempotent within DEDUPE_WINDOW_SECONDS: a byte-identical meal logged
    again in that window returns the original row instead of writing a
    second one. A duplicated A2A delegation replays this tool with the same
    arguments, and every later daily summary would count the meal twice --
    silently, because nothing about a double row looks wrong.
    """
    with _connect() as conn:
        _ensure_created_at(conn)
        existing = _recent_duplicate(
            conn, "food_logs",
            "item_name = ? AND serving_g = ? AND calories = ? "
            "AND protein_g = ? AND carbs_g = ? AND fat_g = ?",
            (name, serving_g, calories, protein_g, carbs_g, fat_g),
        )
        if existing is not None:
            return existing, True

        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO food_logs (
                timestamp, item_name, serving_g, calories, protein_g, carbs_g,
                fat_g, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.date.today().isoformat(), name, serving_g, calories,
                protein_g, carbs_g, fat_g,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid), False


def insert_workout(
    activity: str, duration_min: int, calories: float
) -> tuple[int, bool]:
    """Log a workout. Returns (row id, whether this repeated a recent entry).

    Same idempotency as insert_food, for the same reason.
    """
    with _connect() as conn:
        _ensure_created_at(conn)
        existing = _recent_duplicate(
            conn, "workout_logs",
            "activity = ? AND duration_min = ? AND calories_burned = ?",
            (activity, duration_min, calories),
        )
        if existing is not None:
            return existing, True

        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO workout_logs (
                timestamp, activity, duration_min, calories_burned, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.date.today().isoformat(), activity, duration_min, calories,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid), False


def upsert_user_targets(
    target_daily_calories: float,
    target_protein_g: float,
    target_carbs_g: float,
    target_fat_g: float,
    goal: str,
) -> None:
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_targets (
                id, target_daily_calories, target_protein_g, target_carbs_g,
                target_fat_g, goal, updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_daily_calories = excluded.target_daily_calories,
                target_protein_g = excluded.target_protein_g,
                target_carbs_g = excluded.target_carbs_g,
                target_fat_g = excluded.target_fat_g,
                goal = excluded.goal,
                updated_at = excluded.updated_at
            """,
            (
                target_daily_calories,
                target_protein_g,
                target_carbs_g,
                target_fat_g,
                goal,
                datetime.datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


def get_user_targets() -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT target_daily_calories, target_protein_g, target_carbs_g,
                   target_fat_g, goal, updated_at
            FROM user_targets WHERE id = 1
            """
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "target_daily_calories": row["target_daily_calories"],
            "target_protein_g": row["target_protein_g"],
            "target_carbs_g": row["target_carbs_g"],
            "target_fat_g": row["target_fat_g"],
            "goal": row["goal"],
            "updated_at": row["updated_at"],
        }


def query_daily_summary(date_str: Optional[str] = None) -> Dict[str, Any]:
    target_date = date_str or datetime.date.today().isoformat()
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(calories), 0.0),
                COALESCE(SUM(protein_g), 0.0),
                COALESCE(SUM(carbs_g), 0.0),
                COALESCE(SUM(fat_g), 0.0)
            FROM food_logs WHERE timestamp = ?
            """,
            (target_date,),
        )
        food_totals = cursor.fetchone()

        cursor.execute(
            """
            SELECT COALESCE(SUM(calories_burned), 0.0)
            FROM workout_logs WHERE timestamp = ?
            """,
            (target_date,),
        )
        burned = cursor.fetchone()[0]

    return {
        "date": target_date,
        "calories_consumed": round(float(food_totals[0]), 1),
        "protein_g": round(float(food_totals[1]), 1),
        "carbs_g": round(float(food_totals[2]), 1),
        "fat_g": round(float(food_totals[3]), 1),
        "calories_burned": round(float(burned), 1),
        "net_calories": round(float(food_totals[0]) - float(burned), 1),
    }
