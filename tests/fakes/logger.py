from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# logger fake that echoes text and records undo ids
class FakeLogger:
    # start with no undo taps
    def __init__(self) -> None:
        self.undone: list[str] = []

    # echo the text back
    async def log(self, inbound: Inbound, transport: Transport) -> None:
        await transport.send_text(inbound.user_id, inbound.text or "")

    # record the undo id
    async def undo(self, inbound: Inbound, entry_id: str, transport: Transport) -> None:
        self.undone.append(entry_id)
