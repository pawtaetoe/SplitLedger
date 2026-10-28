from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# group bill fake that records each receipt it was asked to split
class FakeGroup:
    # no calls yet
    def __init__(self) -> None:
        self.calls: list[str] = []

    # record the split
    async def from_receipt(
        self, inbound: Inbound, transport: Transport, ways: int | None = None
    ) -> None:
        self.calls.append(f"split {ways}")

    # record an even split of the latest bill
    async def split_evenly(self, inbound: Inbound, transport: Transport, ways: int) -> None:
        self.calls.append(f"even {ways}")
