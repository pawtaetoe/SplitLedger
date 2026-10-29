from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.handlers.currency import TYPED


# currency fake that records what it was asked to do
class FakeCurrency:
    # start with no calls
    def __init__(self) -> None:
        self.calls: list[str] = []

    # record the list being shown
    async def ask(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("ask")

    # record a tap
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append(f"button {button_id}")

    # record a typed change, only for text that is one
    async def typed(self, inbound: Inbound, text: str, transport: Transport) -> bool:
        if TYPED.match(text.strip()) is None:
            return False
        self.calls.append(f"typed {text}")
        return True
