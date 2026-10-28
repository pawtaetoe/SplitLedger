from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# plan manager fake that records what it was asked to do
class FakePlans:
    # nothing waiting and no calls yet
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.waiting = False

    # record the greeting check
    async def welcome_if_new(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("welcome_if_new")

    # whether the next text is an income reply
    def waiting_for_text(self, wa_user_id: str) -> bool:
        return self.waiting

    # record a plan display
    async def show(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("show")

    # record a plan button
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append(f"button {button_id}")

    # record an income reply
    async def on_text(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("on_text")
