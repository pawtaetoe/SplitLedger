from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# dashboard link fake that counts the links sent
class FakeDash:
    # no links yet
    def __init__(self) -> None:
        self.sent = 0
        self.pages: list[str] = []

    # count one link and the page it opens
    async def send(self, inbound: Inbound, transport: Transport, page: str = "me") -> None:
        self.sent += 1
        self.pages.append(page)
