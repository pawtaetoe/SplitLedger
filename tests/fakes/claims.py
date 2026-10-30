from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# claim chat fake that records what reached it
class FakeClaims:
    # no calls yet
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.open = False

    # record a claim by code
    async def by_code(self, inbound: Inbound, text: str, transport: Transport) -> None:
        self.calls.append(f"code {text}")

    # record a claim in words, with the bill code when one was given
    async def free_text(
        self, inbound: Inbound, text: str, transport: Transport, code: str | None = None,
        quiet: bool = False
    ) -> None:
        self.calls.append(f"words {text}" + (f" on {code}" if code else ""))

    # record a claim button
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append(f"button {button_id}")

    # whether bare numbers count as claims, set by tests
    async def has_bill(self, wa_user_id: str) -> bool:
        return self.open

    # record numbers taken back
    async def unclaim(self, inbound: Inbound, text: str, transport: Transport) -> None:
        self.calls.append(f"unclaim {text}")

    # record bare numbers
    async def numbers(self, inbound: Inbound, text: str, transport: Transport) -> None:
        self.calls.append(f"numbers {text}")
