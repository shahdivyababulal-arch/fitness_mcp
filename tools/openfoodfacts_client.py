"""Open Food Facts lookup.

Kept as a thin adapter so `server.py` stays free of product-ranking /
nutriment-normalization details.

Two different services, because Open Food Facts split them:

  barcode lookup  the SDK's product.get -> /api/v2/product/<code>.json
  text search     search.openfoodfacts.org (Search-a-licious)

The SDK's `product.text_search` is deliberately not used. It calls the legacy
`/cgi/search.pl`, which now answers 503 consistently -- not intermittently,
and not because of the user agent: a barcode fetch with the same headers
returns 200 in the same second. Text search moved to Search-a-licious and the
CGI endpoint is on its way out. That 503 is what made the agent invent macros
and log them, so the endpoint and the honesty of the failure are one fix.
"""

from __future__ import annotations
from functools import lru_cache
from typing import Any, Dict, List

import openfoodfacts
import requests

from config import settings

SEARCH_URL = "https://search.openfoodfacts.org/search"

# Long enough for a slow provider, short enough that a hung lookup does not
# sit inside a delegation the travel agent is waiting on.
_SEARCH_TIMEOUT_SECONDS = 15

# Returned with every failure. The model's instinct on a failed nutrition
# lookup is to fall back on what it "knows" and carry on -- we watched it
# invent 588 kcal/100g for peanut butter and write the derived total to the
# database, twice, with different numbers. Saying so in the payload is more
# reliable than saying it in the prompt alone, because the tool result is
# what the model is reasoning about at that moment.
_NO_ESTIMATE = (
    "Do not substitute typical or remembered values for this item, and do "
    "not log it. Tell the user the nutrition lookup failed and that they can "
    "retry or supply a barcode."
)


def _failure(message: str) -> Dict[str, Any]:
    return {"status": "error", "message": message, "instruction": _NO_ESTIMATE}


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def _api() -> openfoodfacts.API:
    return openfoodfacts.API(user_agent=settings.off_user_agent)


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


def _significant_tokens(query: str) -> set[str]:
    """Query words worth matching a product name against.

    Two characters or fewer are dropped: they match almost anything, which
    defeats the point of the relevance check below.
    """
    tokens = {token.strip(",.()") for token in query.lower().split()}
    significant = {token for token in tokens if len(token) > 2}
    return significant or {token for token in tokens if token}


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
        return _failure("food_query must be a non-empty string.")

    # Barcode-looking queries: prefer exact product fetch.
    if query.isdigit() and len(query) >= 8:
        product = _api().product.get(
            query,
            fields=["code", "product_name", "nutriments"],
        )
        if not product:
            return _failure(f"No product found for barcode '{query}'.")
        macros = _extract_macros(product)
        if macros is None:
            name = product.get("product_name") or query
            return _failure(f"Found '{name}' but nutriment data is missing.")
        return {
            "status": "success",
            "item_name": (product.get("product_name") or query).title(),
            "code": product.get("code") or query,
            "basis": "per 100g",
            **macros,
        }

    try:
        response = requests.get(
            SEARCH_URL,
            params={"q": query, "page_size": 20},
            headers={"User-Agent": settings.off_user_agent},
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        # Search-a-licious calls them `hits`; each one is shaped like a
        # product, so the ranking and macro extraction below are unchanged.
        products: List[Dict[str, Any]] = response.json().get("hits") or []
    except requests.RequestException as error:
        return _failure(f"The nutrition database is unavailable: {error}")
    except ValueError as error:
        return _failure(f"The nutrition database returned malformed data: {error}")

    if not products:
        return _failure(f"No food matching '{query}' found.")

    # Search-a-licious always returns its nearest guesses, however far off.
    # The legacy endpoint returned nothing for a query it did not recognise,
    # so switching search services introduced a new way to be confidently
    # wrong: "zzzzqqq not a food" came back as Organic Large Raw Whole
    # Cashews, complete with macros, which the agent would have logged. A
    # product whose name shares no word with the query is not a match.
    query_tokens = _significant_tokens(query)
    relevant = [p for p in products if _score(p, query_tokens)[0] > 0]
    if not relevant:
        best = (products[0].get("product_name") or "?").strip()
        return _failure(
            f"No food matching '{query}' found. The nearest result was "
            f"'{best}', which does not match what was asked for."
        )

    ranked = sorted(relevant, key=lambda p: _score(p, query_tokens), reverse=True)

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

    return _failure(
        f"Found products for '{query}' but nutriment data is missing. "
        "Try a more specific product name or barcode."
    )
