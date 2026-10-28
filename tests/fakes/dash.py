from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# dashboard link fake that counts the links sent
class FakeDash:
    # no links yet
    def __init__(self) -> None:
        self.sent = 0

    # count one link
    async def send(self, inbound: Inbound, transport: Transport) -> None:
        self.sent += 1
