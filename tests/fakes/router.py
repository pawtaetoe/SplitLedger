from typing import Any

from splitledger.handlers.router import MessageReader, Router
from splitledger.llm.schemas import GLOBAL, Locale
from tests.fakes.assistant import FakeAssistant
from tests.fakes.bills import FakeBills
from tests.fakes.claims import FakeClaims
from tests.fakes.dash import FakeDash
from tests.fakes.group import FakeGroup
from tests.fakes.llm import FakeLLM
from tests.fakes.logger import FakeLogger
from tests.fakes.plans import FakePlans


# a router with a fake for every handler not given, so new handlers touch one place
def fake_router(llm: Any = None, locale: Locale = GLOBAL, **given: Any) -> Router:
    return Router(
        given.get("logger") or FakeLogger(), MessageReader(llm or FakeLLM(), locale),
        given.get("assistant") or FakeAssistant(), given.get("plans") or FakePlans(),
        given.get("bills") or FakeBills(), given.get("group") or FakeGroup(),
        given.get("claims") or FakeClaims(), given.get("dash") or FakeDash()
    )
