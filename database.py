"""SQLite schema, connection helpers, and CRUD for biometrics logs."""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

# Keep the DB at fitness_agent/fitness_data.db (package parent).
DB_PATH = Path(__file__).resolve().parent.parent / "fitness_data.db"


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
                fat_g REAL DEFAULT 0.0
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
                calories_burned REAL NOT NULL
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


def insert_food(
    name: str,
    serving_g: float,
    calories: float,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
) -> int:
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO food_logs (
                timestamp, item_name, serving_g, calories, protein_g, carbs_g, fat_g
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.date.today().isoformat(),
                name,
                serving_g,
                calories,
                protein_g,
                carbs_g,
                fat_g,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def insert_workout(activity: str, duration_min: int, calories: float) -> int:
    with _connect() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO workout_logs (timestamp, activity, duration_min, calories_burned)
            VALUES (?, ?, ?, ?)
            """,
            (datetime.date.today().isoformat(), activity, duration_min, calories),
        )
        conn.commit()
        return int(cursor.lastrowid)


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
