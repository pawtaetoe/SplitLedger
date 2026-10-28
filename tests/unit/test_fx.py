import httpx
import respx

from splitledger.split.fx import FX_URL, Rates, convert


# rates are turned around into the base, cached, and the last good ones kept on failure
@respx.mock
async def test_rates():
    route = respx.get(url__startswith=FX_URL).mock(
        return_value=httpx.Response(200, json={"base": "SGD", "rates": {"CHF": 0.5}})
    )
    rates = Rates(client=httpx.AsyncClient())
    assert await rates.to("SGD") == {"CHF": 2.0, "SGD": 1.0}
    assert await rates.to("SGD") == {"CHF": 2.0, "SGD": 1.0}
    assert route.call_count == 1
    # the first source failing falls back to the second, both failing keeps the base only
    route.mock(return_value=httpx.Response(500))
    backup = respx.get("https://open.er-api.com/v6/latest/USD").mock(
        return_value=httpx.Response(200, json={"rates": {"USD": 1, "CHF": 0.8}})
    )
    assert await Rates(client=httpx.AsyncClient()).to("USD") == {"USD": 1.0, "CHF": 1.25}
    backup.mock(return_value=httpx.Response(500))
    assert await Rates(client=httpx.AsyncClient()).to("USD") == {"USD": 1.0}


# cents convert at the rate, a currency with no rate is left alone
def test_convert():
    assert convert(12400, "CHF", {"CHF": 1.5}) == 18600
    assert convert(100, "VND", {"CHF": 1.5}) is None


# with a key the keyed source goes first, reads conversion_rates and keeps the key out of logs
@respx.mock
async def test_keyed_source(caplog):
    keyed = respx.get("https://v6.exchangerate-api.com/v6/secret-key/latest/SGD").mock(
        return_value=httpx.Response(200, json={"conversion_rates": {"SGD": 1, "CHF": 0.5}})
    )
    assert await Rates("secret-key", httpx.AsyncClient()).to("SGD") == {"SGD": 1.0, "CHF": 2.0}
    keyed.mock(return_value=httpx.Response(403))
    respx.get(url__startswith=FX_URL).mock(
        return_value=httpx.Response(200, json={"rates": {"CHF": 0.5}})
    )
    assert (await Rates("secret-key", httpx.AsyncClient()).to("SGD"))["CHF"] == 2.0
    assert "secret-key" not in caplog.text and "status 403" in caplog.text
