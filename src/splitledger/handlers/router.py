from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport


# routes one inbound message, echo only for now
class Router:
    # echo the message back through the transport
    async def handle(self, inbound: Inbound, transport: Transport) -> None:
        if inbound.kind == "image" and inbound.media_ref:
            png = await transport.fetch_media(inbound.media_ref)
            await transport.send_image(inbound.user_id, png, caption=inbound.text)
            return
        if inbound.kind == "button":
            await transport.send_text(inbound.user_id, inbound.button_id or "")
            return
        if inbound.kind == "audio":
            await transport.send_text(inbound.user_id, "[audio]")
            return
        await transport.send_text(inbound.user_id, inbound.text or "")
