from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# assistant fake that records each question it was asked
class FakeAssistant:
    # start with no questions
    def __init__(self) -> None:
        self.asked: list[str] = []

    # record the question
    async def answer(self, inbound: Inbound, transport: Transport) -> None:
        self.asked.append(inbound.text or "")
