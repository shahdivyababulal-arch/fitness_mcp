"""log_entry must survive being called twice with the same arguments.

A duplicated A2A delegation replays the whole fitness task, tool calls
included -- observed twice, 21 seconds apart. Two identical rows in
food_logs are counted twice by every later daily summary, and nothing
about the second row looks wrong, so the error is silent and permanent.
"""

import datetime

import pytest

from tools import database


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_database()
    yield


def _log():
    return database.insert_food("Peanut Butter", 32.0, 180.0, 7.2, 5.3, 15.4)


def test_an_identical_meal_logged_twice_writes_one_row():
    first_id, first_dup = _log()
    second_id, second_dup = _log()

    assert first_dup is False
    assert second_dup is True, "the replay should be recognised"
    assert second_id == first_id, "it should reuse the original row"

    rows = database.query_daily_summary()
    assert rows["calories_consumed"] == 180.0, "the meal must be counted once"


def test_a_different_meal_is_still_logged():
    _log()
    other_id, dup = database.insert_food("Oats", 50.0, 190.0, 6.0, 33.0, 3.0)
    assert dup is False
    assert other_id


def test_the_same_food_in_a_different_portion_is_not_a_duplicate():
    """Two helpings of different size are two real entries."""
    _log()
    _, dup = database.insert_food("Peanut Butter", 64.0, 360.0, 14.4, 10.6, 30.8)
    assert dup is False


def test_a_genuine_second_helping_later_is_logged(monkeypatch):
    """Idempotency must not become "you may only eat this once today".

    The window separates a replay seconds apart from a real repeat, which
    people do minutes or hours later.
    """
    _log()

    real = datetime.datetime
    later = real.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=database.DEDUPE_WINDOW_SECONDS + 60)

    class _Later(real):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(database.datetime, "datetime", _Later)
    _, dup = _log()
    assert dup is False, "a helping outside the window is a real second entry"


def test_workouts_deduplicate_too():
    first_id, first_dup = database.insert_workout("running", 30, 300.0)
    second_id, second_dup = database.insert_workout("running", 30, 300.0)
    assert first_dup is False and second_dup is True
    assert second_id == first_id


def test_rows_written_before_the_column_existed_never_match():
    """An upgraded database must not collapse a day's history.

    Pre-migration rows have created_at NULL; treating NULL as "recent"
    would make the first old entry swallow every later identical one.
    """
    with database._connect() as conn:
        conn.execute(
            "INSERT INTO food_logs (timestamp, item_name, serving_g, calories,"
            " protein_g, carbs_g, fat_g, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (datetime.date.today().isoformat(), "Peanut Butter", 32.0, 180.0,
             7.2, 5.3, 15.4),
        )
        conn.commit()

    _, dup = _log()
    assert dup is False, "a legacy row with no timestamp must not match"


def test_the_window_is_short_enough_to_be_a_replay_guard():
    """Long enough to catch a duplicated delegation, short enough that a
    real second helping is not swallowed."""
    assert 30 <= database.DEDUPE_WINDOW_SECONDS <= 300


def test_log_entry_is_still_a_registered_mcp_tool():
    """Guards a mistake I made adding the helper above.

    Inserting a function between `@mcp.tool()` and `def log_entry` moved
    the decorator onto the helper. Every database-level test still passed
    -- they call `tools.database` directly -- while over the wire the
    server answered "Unknown tool: log_entry". Only a protocol-level call
    caught it.
    """
    import server

    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert "log_entry" in names
    assert "_logged_result" not in names, "a helper leaked into the tool surface"


def test_log_entry_reports_the_reuse_to_its_caller():
    """`success`, not an error: the entry the caller wanted is in the log.

    Reporting failure would push a model into retrying, which makes the
    duplicate worse. The flag lets an agent avoid telling the user they
    ate twice.
    """
    import server

    fn = server.log_entry.fn if hasattr(server.log_entry, "fn") else server.log_entry
    args = dict(entry_type="meal", name="Oats", amount_or_duration=50,
                calories=190, protein_g=6.0, carbs_g=33.0, fat_g=3.0)
    first, second = fn(**args), fn(**args)

    assert first["status"] == second["status"] == "success"
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["entry_id"] == first["entry_id"]
    assert "already logged" in second["note"].lower()
