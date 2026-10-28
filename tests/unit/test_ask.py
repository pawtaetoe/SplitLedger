from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from splitledger.handlers.ask import SpendTools, category_arg, day_range, guarded

SG = ZoneInfo("Asia/Singapore")


# local days become a utc range with an exclusive end
def test_day_range_in_timezone():
    first, after = day_range({"start": "2026-10-27", "end": "2026-10-27"}, SG)
    assert first.astimezone(UTC) == datetime(2026, 10, 26, 16, tzinfo=UTC)
    assert after.astimezone(UTC) == datetime(2026, 10, 27, 16, tzinfo=UTC)


# bad dates, reversed ranges and ranges over a year are refused
@pytest.mark.parametrize(
    "args",
    [
        {"start": "yesterday", "end": "2026-10-27"},
        {"start": "2026-10-28", "end": "2026-10-27"},
        {"start": "2025-01-01", "end": "2026-10-27"},
        {}
    ]
)
def test_day_range_refuses(args):
    with pytest.raises(ValueError):
        day_range(args, SG)


# categories must come from the fixed list
def test_category_arg():
    assert category_arg({"category": "Dining"}) == "Dining"
    assert category_arg({}) is None
    with pytest.raises(ValueError):
        category_arg({"category": "Sushi"})


# a query error goes back to the model instead of crashing the loop
async def test_guarded_returns_error():
    async def broken(args):
        raise ValueError("end must be on or after start")

    assert await guarded(broken)({}) == {"error": "end must be on or after start"}


# every tool names its arguments so the model knows what to send
def test_tools_have_schemas():
    tools = SpendTools(None, None, SG, None).tools()
    assert [tool.name for tool in tools] == [
        "sum_spend", "top_expenses", "list_expenses", "who_owes_me", "plan_status"
    ]
    for tool in tools[:3]:
        assert tool.parameters["required"] == ["start", "end"]
    assert "category" in tools[0].parameters["properties"]
    assert "n" in tools[1].parameters["properties"]
