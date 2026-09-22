"""Nutrition lookup: the right endpoint, and honest failures.

Two bugs met here. The legacy `/cgi/search.pl` text search answers 503
permanently, and on that failure the agent invented macros and wrote them to
the database -- twice, with different numbers. Fixing the endpoint removes
the trigger; making the failure explicit removes the behaviour.

These use a stubbed transport: a test that depends on Open Food Facts being
up would fail for the very reason this code exists.
"""

import pytest
import requests

from tools import openfoodfacts_client as off


class _Response:
    def __init__(self, payload=None, status=200, boom=None):
        self._payload = payload or {}
        self.status_code = status
        self._boom = boom

    def raise_for_status(self):
        if self._boom:
            raise self._boom

    def json(self):
        return self._payload


def _hit(name, kcal=500.0, code="123"):
    return {
        "product_name": name,
        "code": code,
        "nutriments": {
            "energy-kcal_100g": kcal,
            "proteins_100g": 20.0,
            "carbohydrates_100g": 30.0,
            "fat_100g": 40.0,
        },
    }


def test_text_search_uses_search_a_licious_not_the_dead_cgi_endpoint(monkeypatch):
    """`/cgi/search.pl` returns 503 consistently; this is the whole fix."""
    seen = {}

    def _fake_get(url, **kwargs):
        seen["url"] = url
        seen["params"] = kwargs.get("params")
        return _Response({"hits": [_hit("Peanut Butter")]})

    monkeypatch.setattr(requests, "get", _fake_get)
    result = off.search_food_nutrition("peanut butter")
    assert result["status"] == "success"
    assert seen["url"] == off.SEARCH_URL
    assert "cgi/search.pl" not in seen["url"]
    assert seen["params"]["q"] == "peanut butter"


def test_a_provider_outage_is_reported_not_estimated(monkeypatch):
    """The failure that started this: 503, then invented macros."""
    def _boom(url, **kwargs):
        raise requests.ConnectionError("503 Service Temporarily Unavailable")

    monkeypatch.setattr(requests, "get", _boom)
    result = off.search_food_nutrition("peanut butter")
    assert result["status"] == "error"
    assert "unavailable" in result["message"].lower()
    # No macro keys may leak out of a failure -- a caller reading them would
    # be reading numbers nobody measured.
    for key in ("calories_kcal", "protein_g", "carbs_g", "fat_g"):
        assert key not in result


def test_every_failure_tells_the_model_not_to_estimate(monkeypatch):
    """The instruction rides in the payload, not only in the prompt.

    The tool result is what the model is reasoning about at the moment it
    decides whether to improvise.
    """
    def _boom(url, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "get", _boom)
    result = off.search_food_nutrition("peanut butter")
    assert "do not log it" in result["instruction"].lower()
    assert "not substitute" in result["instruction"].lower()

    # empty query takes a different branch and must say the same thing
    assert off.search_food_nutrition("")["instruction"] == result["instruction"]


def test_an_irrelevant_fuzzy_match_is_rejected(monkeypatch):
    """Search-a-licious always returns its nearest guess, however far off.

    The legacy endpoint returned nothing for an unrecognised query, so the
    new service introduced a fresh way to be confidently wrong: a nonsense
    query came back as Organic Large Raw Whole Cashews, with macros the
    agent would have logged.
    """
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: _Response({"hits": [_hit("Organic Large Raw Whole Cashews")]}),
    )
    result = off.search_food_nutrition("zzzzqqq")
    assert result["status"] == "error"
    assert "cashews" in result["message"].lower()


def test_a_relevant_match_is_accepted(monkeypatch):
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: _Response({"hits": [
            _hit("Organic Large Raw Whole Cashews"),
            _hit("Smooth Peanut Butter", kcal=588.0),
        ]}),
    )
    result = off.search_food_nutrition("peanut butter")
    assert result["status"] == "success"
    assert result["item_name"] == "Smooth Peanut Butter"
    assert result["calories_kcal"] == 588.0


def test_short_query_words_do_not_count_as_relevance(monkeypatch):
    """Two-letter words match almost anything and would defeat the check."""
    assert off._significant_tokens("a of peanut") == {"peanut"}
    # a query that is only short words still has to match on something
    assert off._significant_tokens("a of") == {"a", "of"}


def test_malformed_json_is_a_failure_not_a_crash(monkeypatch):
    def _bad(url, **kwargs):
        return _Response(boom=None, payload=None)

    class _BadJson(_Response):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(requests, "get", lambda url, **kw: _BadJson())
    result = off.search_food_nutrition("peanut butter")
    assert result["status"] == "error"
    assert "malformed" in result["message"].lower()


def test_the_search_has_a_timeout(monkeypatch):
    """A hung provider must not stall a delegation the travel agent awaits."""
    seen = {}
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: (seen.update(kw), _Response({"hits": [_hit("Peanut Butter")]}))[1],
    )
    off.search_food_nutrition("peanut butter")
    assert seen["timeout"] == off._SEARCH_TIMEOUT_SECONDS
