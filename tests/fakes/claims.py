from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# claim chat fake that records what reached it
class FakeClaims:
    # no calls yet
    def __init__(self) -> None:
        self.calls: list[str] = []

    # record a claim by code
    async def by_code(self, inbound: Inbound, text: str, transport: Transport) -> None:
        self.calls.append(f"code {text}")

    # record a claim in words
    async def free_text(self, inbound: Inbound, text: str, transport: Transport) -> None:
        self.calls.append(f"words {text}")

    # record a claim button
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append(f"button {button_id}")
