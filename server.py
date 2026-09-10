"""FastMCP server exposing health and biometrics tools over Streamable HTTP."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from . import database, openfoodfacts_client
from observability import configure_tracing, traced_tool

_PACKAGE_ROOT = Path(__file__).resolve().parent
_APP_ROOT = _PACKAGE_ROOT.parent
load_dotenv(_APP_ROOT / ".env")

MCP_HOST = os.getenv("FITNESS_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("FITNESS_MCP_PORT", "8003"))

# Keep protocol traffic separate from application logs.
LOG_DIR = _APP_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [SERVER] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log", mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("mcp_server")

database.init_database()
configure_tracing()

mcp = FastMCP("health-biometrics-server", host=MCP_HOST, port=MCP_PORT)


@mcp.tool()
@traced_tool("search_food_nutrition")
def search_food_nutrition(food_query: str) -> Dict[str, Any]:
    """Looks up nutritional profile (calories, protein, carbs, fat per 100g) for any grocery or food item."""
    logger.info("search_food_nutrition requested for: %r", food_query)
    try:
        return openfoodfacts_client.search_food_nutrition(food_query)
    except Exception as err:
        logger.error("Failed to query Open Food Facts: %s", err, exc_info=True)
        return {"status": "error", "message": str(err)}


@mcp.tool()
@traced_tool("calculate_tdee_and_macros")
def calculate_tdee_and_macros(
    weight_kg: float,
    height_cm: float,
    age: int,
    is_male: bool,
    activity_level: str = "moderate",
    goal: str = "maintain",
    persist_targets: bool = True,
) -> Dict[str, Any]:
    """Calculates BMR, TDEE, and macro gram allocations according to body composition and fitness objective.

    When persist_targets is true (default), stores daily calorie/macro targets in SQLite
    so get_daily_biometrics_summary can use them as the budget.
    """
    logger.info(
        "calculate_tdee invoked for %skg, %scm, %syo, goal=%s",
        weight_kg,
        height_cm,
        age,
        goal,
    )

    bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + (5 if is_male else -161)

    multipliers = {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "very_active": 1.725,
    }
    tdee = bmr * multipliers.get(activity_level.lower(), 1.55)

    adjustments = {"cut": -500, "bulk": 300, "maintain": 0}
    target_cals = tdee + adjustments.get(goal.lower(), 0)

    protein_g = weight_kg * 2.0
    fat_g = (target_cals * 0.25) / 9
    carbs_g = max(0.0, (target_cals - (protein_g * 4 + fat_g * 9)) / 4)

    result = {
        "bmr_kcal": round(bmr),
        "tdee_kcal": round(tdee),
        "target_daily_calories": round(target_cals),
        "target_protein_g": round(protein_g),
        "target_carbs_g": round(carbs_g),
        "target_fat_g": round(fat_g),
        "goal_applied": goal,
        "activity_level_applied": activity_level,
    }

    if persist_targets:
        database.upsert_user_targets(
            target_daily_calories=float(result["target_daily_calories"]),
            target_protein_g=float(result["target_protein_g"]),
            target_carbs_g=float(result["target_carbs_g"]),
            target_fat_g=float(result["target_fat_g"]),
            goal=goal,
        )
        result["targets_persisted"] = True
    else:
        result["targets_persisted"] = False

    return result


@mcp.tool()
@traced_tool("log_entry")
def log_entry(
    entry_type: str,
    name: str,
    amount_or_duration: float,
    calories: float,
    protein_g: float = 0.0,
    carbs_g: float = 0.0,
    fat_g: float = 0.0,
) -> Dict[str, Any]:
    """Persists a meal or a workout into SQLite. entry_type must be either 'meal' or 'workout'.

    For meals, amount_or_duration is serving grams. For workouts, it is duration in minutes.
    Scale Open Food Facts per-100g values to the actual portion before logging a meal.
    """
    logger.info("log_entry invoked: type=%s, name=%s, cals=%s", entry_type, name, calories)

    cleaned_name = (name or "").strip()
    if not cleaned_name:
        return {"status": "error", "message": "name must be a non-empty string."}
    if amount_or_duration <= 0:
        return {
            "status": "error",
            "message": "amount_or_duration must be greater than zero.",
        }

    kind = (entry_type or "").lower().strip()
    if kind == "meal":
        row_id = database.insert_food(
            cleaned_name,
            amount_or_duration,
            calories,
            protein_g,
            carbs_g,
            fat_g,
        )
        return {
            "status": "success",
            "entry_id": row_id,
            "logged": (
                f"Meal: {amount_or_duration}g {cleaned_name} ({calories} kcal)"
            ),
        }
    if kind == "workout":
        row_id = database.insert_workout(
            cleaned_name,
            int(amount_or_duration),
            calories,
        )
        return {
            "status": "success",
            "entry_id": row_id,
            "logged": (
                f"Workout: {amount_or_duration} mins of {cleaned_name} (-{calories} kcal)"
            ),
        }
    return {
        "status": "error",
        "message": "Invalid entry_type. Use 'meal' or 'workout'.",
    }


@mcp.tool()
@traced_tool("get_daily_biometrics_summary")
def get_daily_biometrics_summary(
    budget_calories: Optional[float] = None,
) -> Dict[str, Any]:
    """Retrieves current day totals from SQLite and calculates remaining caloric budget.

    If budget_calories is omitted, uses the last persisted TDEE target when available,
    otherwise defaults to 2000 kcal.
    """
    logger.info("get_daily_biometrics_summary invoked")
    summary = database.query_daily_summary()
    targets = database.get_user_targets()

    if budget_calories is None:
        if targets is not None:
            budget = float(targets["target_daily_calories"])
            summary["budget_source"] = "persisted_targets"
        else:
            budget = 2000.0
            summary["budget_source"] = "default"
    else:
        budget = float(budget_calories)
        summary["budget_source"] = "argument"

    summary["budget_calories"] = budget
    summary["remaining_calories"] = round(budget - summary["net_calories"], 1)

    if targets is not None:
        summary["target_protein_g"] = targets["target_protein_g"]
        summary["target_carbs_g"] = targets["target_carbs_g"]
        summary["target_fat_g"] = targets["target_fat_g"]
        summary["goal"] = targets["goal"]

    return summary


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
