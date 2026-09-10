"""Open Food Facts lookup via the official `openfoodfacts` SDK.

Kept as a thin adapter so `server.py` stays free of product-ranking /
nutriment-normalization details. Swap SDK usage here without touching MCP tools.
"""

from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import openfoodfacts
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_USER_AGENT = "ADKHealthAgent/1.0 (contact@example.com)"


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def _api() -> openfoodfacts.API:
    user_agent = os.getenv("OFF_USER_AGENT", DEFAULT_USER_AGENT)
    return openfoodfacts.API(user_agent=user_agent)


def _extract_macros(product: Dict[str, Any]) -> Dict[str, float] | None:
    nutriments = product.get("nutriments")
    if not isinstance(nutriments, dict) or not nutriments:
        return None

    has_energy = (
        "energy-kcal_100g" in nutriments
        or "energy-kcal" in nutriments
        or "energy_100g" in nutriments
        or "energy" in nutriments
    )
    has_macro = any(
        key in nutriments
        for key in (
            "proteins_100g",
            "carbohydrates_100g",
            "fat_100g",
            "proteins",
            "carbohydrates",
            "fat",
        )
    )
    if not has_energy and not has_macro:
        return None

    kcal = nutriments.get("energy-kcal_100g")
    if kcal is None:
        kcal = nutriments.get("energy-kcal")
    if kcal is None:
        energy_kj = nutriments.get("energy_100g", nutriments.get("energy"))
        kcal = (_as_float(energy_kj) / 4.184) if energy_kj is not None else 0.0

    return {
        "calories_kcal": round(_as_float(kcal), 1),
        "protein_g": round(
            _as_float(nutriments.get("proteins_100g", nutriments.get("proteins"))),
            1,
        ),
        "carbs_g": round(
            _as_float(
                nutriments.get("carbohydrates_100g", nutriments.get("carbohydrates"))
            ),
            1,
        ),
        "fat_g": round(
            _as_float(nutriments.get("fat_100g", nutriments.get("fat"))),
            1,
        ),
    }


def _score(product: Dict[str, Any], query_tokens: set[str]) -> tuple[int, int]:
    name = (product.get("product_name") or "").lower()
    token_hits = sum(1 for token in query_tokens if token in name)
    nutriments = product.get("nutriments") or {}
    has_kcal = 1 if (
        nutriments.get("energy-kcal_100g") is not None
        or nutriments.get("energy_100g") is not None
    ) else 0
    return (token_hits, has_kcal)


def search_food_nutrition(food_query: str) -> Dict[str, Any]:
    """Looks up nutritional profile per 100g for a grocery/food item."""
    query = (food_query or "").strip()
    if not query:
        return {"status": "error", "message": "food_query must be a non-empty string."}

    # Barcode-looking queries: prefer exact product fetch.
    if query.isdigit() and len(query) >= 8:
        product = _api().product.get(
            query,
            fields=["code", "product_name", "nutriments"],
        )
        if not product:
            return {"status": "error", "message": f"No product found for barcode '{query}'."}
        macros = _extract_macros(product)
        if macros is None:
            name = product.get("product_name") or query
            return {
                "status": "error",
                "message": f"Found '{name}' but nutriment data is missing.",
            }
        return {
            "status": "success",
            "item_name": (product.get("product_name") or query).title(),
            "code": product.get("code") or query,
            "basis": "per 100g",
            **macros,
        }

    result = _api().product.text_search(query)
    products: List[Dict[str, Any]] = result.get("products") or []
    if not products:
        return {"status": "error", "message": f"No food matching '{query}' found."}

    query_tokens = {token for token in query.lower().split() if token}
    ranked = sorted(products, key=lambda p: _score(p, query_tokens), reverse=True)

    for product in ranked:
        macros = _extract_macros(product)
        if macros is None:
            continue
        return {
            "status": "success",
            "item_name": (product.get("product_name") or query).title(),
            "code": product.get("code"),
            "basis": "per 100g",
            **macros,
        }

    return {
        "status": "error",
        "message": (
            f"Found products for '{query}' but nutriment data is missing. "
            "Try a more specific product name or barcode."
        ),
    }
