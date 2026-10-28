from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# upcoming bills fake that records what it was asked to do
class FakeBills:
    # no calls yet
    def __init__(self) -> None:
        self.calls: list[str] = []

    # record a list display
    async def show(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("show")

    # record a bills button
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append(f"button {button_id}")
