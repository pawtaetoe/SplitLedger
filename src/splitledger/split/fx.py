import logging
import time
from typing import Protocol

import httpx

LOG = logging.getLogger(__name__)
# daily rates as url, query name for the base and the field holding units per one base
# the keyed source first when a key is set, then two that need no key
KEYED = ("https://v6.exchangerate-api.com/v6/{key}/latest/{base}", None, "conversion_rates")
SOURCES = (
    ("https://api.frankfurter.dev/v1/latest", "from", "rates"),
    ("https://open.er-api.com/v6/latest/{base}", None, "rates")
)
FX_URL = SOURCES[0][0]
CACHE_SECONDS = 12 * 3600
TIMEOUT = 5.0


# anything that says how much one unit of each currency is worth in a base currency
class RateSource(Protocol):
    # rates into the base, the base itself is 1
    async def to(self, base: str) -> dict[str, float]: ...


# today's rates, fetched at most twice a day, the last good ones kept if a fetch fails
class Rates:
    # an optional api key and an optional client so tests can stub the network
    def __init__(self, key: str = "", client: httpx.AsyncClient | None = None) -> None:
        self._key = key
        self._client = client
        self._cache: dict[str, tuple[float, dict[str, float]]] = {}

    # how much one unit of each currency is worth in the base currency
    async def to(self, base: str) -> dict[str, float]:
        cached = self._cache.get(base)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        sources = (KEYED, *SOURCES) if self._key else SOURCES
        for url, param, field in sources:
            target = url.format(key=self._key, base=base)
            try:
                rates = await self._fetch(target, {param: base} if param else {}, field)
            except (httpx.HTTPError, KeyError, ValueError, TypeError, ZeroDivisionError) as error:
                # the host only, a keyed url must never reach the logs
                LOG.warning("exchange rates from %s failed with %s", host(url), describe(error))
                continue
            rates[base] = 1.0
            self._cache[base] = (time.monotonic(), rates)
            return rates
        return cached[1] if cached else {base: 1.0}

    # one source's rates turned into the value of one unit in the base
    async def _fetch(self, url: str, params: dict[str, str], field: str) -> dict[str, float]:
        if self._client is not None:
            response = await self._client.get(url, params=params, follow_redirects=True)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, params=params)
        response.raise_for_status()
        per_base = response.json()[field]
        return {code: 1 / float(rate) for code, rate in per_base.items() if rate}


# the host of a source url
def host(url: str) -> str:
    return url.split("/")[2]


# an error in a few words, the status for http errors and never the url
def describe(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"status {error.response.status_code}"
    return type(error).__name__


# cents in another currency as cents in the base, none when there is no rate
def convert(cents: int, code: str, rates: dict[str, float]) -> int | None:
    rate = rates.get(code)
    return round(cents * rate) if rate is not None else None
