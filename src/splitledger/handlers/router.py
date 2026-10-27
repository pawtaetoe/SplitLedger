from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.session import SessionFactory
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.client import MetaLLM
from splitledger.llm.schemas import GLOBAL
from splitledger.llm.schemas_sg import SG
from splitledger.settings import settings

# add a region with a new schemas module and list it here
LOCALES = {"global": GLOBAL, "sg": SG}


# routes one inbound message, text logs and the rest still echoes
class Router:
    # expense logger for text and undo taps
    def __init__(self, logger: ExpenseLogger) -> None:
        self._logger = logger

    # pick the handler for this message
    async def handle(self, inbound: Inbound, transport: Transport) -> None:
        if inbound.kind == "text":
            await self._logger.log(inbound, transport)
            return
        button_id = inbound.button_id or ""
        if inbound.kind == "button" and button_id.startswith("undo:"):
            await self._logger.undo(inbound, button_id.removeprefix("undo:"), transport)
            return
        if inbound.kind == "image" and inbound.media_ref:
            png = await transport.fetch_media(inbound.media_ref)
            await transport.send_image(inbound.user_id, png, caption=inbound.text)
            return
        if inbound.kind == "button":
            await transport.send_text(inbound.user_id, button_id)
            return
        if inbound.kind == "audio":
            await transport.send_text(inbound.user_id, "[audio]")
            return
        await transport.send_text(inbound.user_id, inbound.text or "")


message_router = Router(ExpenseLogger(MetaLLM(), SessionFactory, LOCALES[settings.locale]))
