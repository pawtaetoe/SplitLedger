import json
from datetime import date

import httpx
import pytest
import respx

from splitledger.channels.whatsapp import WhatsAppTransport, parse_inbound
from splitledger.handlers.plan import parse_income
from splitledger.plans.frameworks import PRESETS, pay_yourself_first
from splitledger.plans.quota import days_left, period_key, period_start, quota_cents

WEDNESDAY = date(2026, 10, 28)


# every framework splits the whole income and knows its savings buckets
def test_presets_sum_to_100():
    for framework in [*PRESETS.values(), pay_yourself_first(15)]:
        assert sum(framework.buckets.values()) == 100
        assert set(framework.category_map.values()) <= set(framework.buckets)
    assert PRESETS["503020"].savings() == ["Savings"]
    assert PRESETS["60"].savings() == ["Irregular", "Retirement", "Long-term"]


# monthly shares and weekly shares as a year of months over 52 weeks
def test_quota_cents():
    assert quota_cents(450000, 30, "monthly") == 135000
    assert quota_cents(450000, 30, "weekly") == 31153


# weeks start on monday and months on the first
def test_periods():
    assert period_start(WEDNESDAY, "weekly") == date(2026, 10, 26)
    assert period_start(WEDNESDAY, "monthly") == date(2026, 10, 1)
    assert period_key(WEDNESDAY, "weekly") == "2026-W44"
    assert period_key(WEDNESDAY, "monthly") == "2026-10"
    assert period_key(date(2026, 11, 2), "weekly") == "2026-W45"
    assert days_left(WEDNESDAY, "weekly") == 5
    assert days_left(WEDNESDAY, "monthly") == 4


# plain amounts in the usual shapes are read and anything else is refused
@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("4500", 450000),
        ("$4,500.50", 450050),
        ("SGD 4500", 450000),
        ("4.5k", 450000),
        ("abc", None),
        ("0", None),
        ("4500 and 300", None)
    ]
)
def test_parse_income(text, cents):
    assert parse_income(text) == cents


# more than three choices go to whatsapp as a pick list
@respx.mock
async def test_whatsapp_list_for_many_choices():
    route = respx.post("https://graph.facebook.com/v21.0/999/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
    )
    transport = WhatsAppTransport(access_token="token", phone_number_id="999")
    choices = [("a", "One"), ("b", "Two"), ("c", "Three"), ("d", "Four")]
    await transport.send_buttons("6581234567", "Pick", choices)
    interactive = json.loads(route.calls.last.request.content)["interactive"]
    assert interactive["type"] == "list"
    assert [row["id"] for row in interactive["action"]["sections"][0]["rows"]] == list("abcd")


# a list reply comes back as a button tap
def test_list_reply_parses_as_button():
    message = {
        "from": "6581234567", "id": "wamid.list1", "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "plan:fw:60", "title": "60%"}}
    }
    payload = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
    inbound = parse_inbound(payload)[0]
    assert (inbound.kind, inbound.button_id) == ("button", "plan:fw:60")
