from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# logger fake that echoes text and images and records button ids
class FakeLogger:
    # start with no undo taps
    def __init__(self) -> None:
        self.undone: list[str] = []
        self.confirmed: list[str] = []

    # echo the text back
    async def log(self, inbound: Inbound, transport: Transport) -> None:
        await transport.send_text(inbound.user_id, inbound.text or "")

    # record the undo id
    async def undo(self, inbound: Inbound, entry_id: str, transport: Transport) -> None:
        self.undone.append(entry_id)

    # echo the image back
    async def log_receipt(self, inbound: Inbound, transport: Transport) -> None:
        image = await transport.fetch_media(inbound.media_ref or "")
        await transport.send_image(inbound.user_id, image, caption=inbound.text)

    # mark the voice note as heard
    async def log_voice(self, inbound: Inbound, transport: Transport) -> None:
        await transport.send_text(inbound.user_id, "[audio]")

    # record the confirmed receipt key
    async def confirm_receipt(self, inbound: Inbound, key: str, transport: Transport) -> None:
        self.confirmed.append(key)
