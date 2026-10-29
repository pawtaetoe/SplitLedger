from splitledger.handlers.currency import currency_for, timezone_for
from splitledger.handlers.router import MessageReader
from splitledger.llm.schemas import GLOBAL, Regions, by_country_code
from splitledger.llm.schemas_sg import SG
from tests.fakes import FakeLLM


# the country calling code picks the currency, the longest match wins, unknown falls back
def test_currency_for():
    assert currency_for("6581234567", "USD") == "SGD"
    assert currency_for("60123456789", "SGD") == "MYR"
    assert currency_for("85212345678", "SGD") == "HKD"
    assert currency_for("14155238886", "SGD") == "USD"
    assert currency_for("+44 7700 900123", "SGD") == "GBP"
    assert currency_for("999123", "SGD") == "SGD"


# the timezone follows the number too
def test_timezone_for():
    assert timezone_for("6581234567", "UTC") == "Asia/Singapore"
    assert timezone_for("447700900123", "UTC") == "Europe/London"
    assert timezone_for("999123", "UTC") == "UTC"


# a +65 number gets the singapore pack, so kopi is food and S$ is money, others get the default
def test_regions_by_number():
    regions = Regions(GLOBAL, {"65": SG})
    assert regions.for_user("6581234567") is SG
    assert regions.for_user("447700900123") is GLOBAL
    reader = MessageReader(FakeLLM(), regions)
    assert reader.by_rules("kopi 2.20", "6581234567").intent == "log"
    assert reader.by_rules("kopi 2.20", "447700900123").intent == "unclear"
    assert by_country_code("85212345678", {"85": "a", "852": "b"}, "c") == "b"
