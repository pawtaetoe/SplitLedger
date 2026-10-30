from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# group bill fake that records each receipt it was asked to split
class FakeGroup:
    # no calls yet
    def __init__(self) -> None:
        self.calls: list[str] = []
        # what from_receipt returns, a bill like object or none
        self.bill: object = None

    # record the split
    async def from_receipt(
        self, inbound: Inbound, transport: Transport, ways: int | None = None,
        unsure: bool = False
    ) -> object:
        self.calls.append(f"split {ways}" + (" unsure" if unsure else ""))
        return self.bill

    # record a split of a receipt already read
    async def from_draft(
        self, inbound: Inbound, transport: Transport, receipt: object, ways: int | None = None,
        unsure: bool = False
    ) -> object:
        self.calls.append(f"draft {ways}" + (" unsure" if unsure else ""))
        return self.bill

    # record an even split of the latest bill
    async def split_evenly(self, inbound: Inbound, transport: Transport, ways: int) -> None:
        self.calls.append(f"even {ways}")

    # record a close button
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append(f"button {button_id}")

    # record a close in words
    async def close_latest(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("close")
