from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerSource
from splitledger.llm.schemas import ExpenseDraft, ReceiptDraft


# logger fake that echoes text and images and records every call
class FakeLogger:
    # start with no undo taps
    def __init__(self) -> None:
        self.undone: list[str] = []
        self.confirmed: list[str] = []
        self.calls: list[str] = []
        self.drafts: list[tuple[ExpenseDraft, LedgerSource]] = []
        self.heard: str | None = None
        # what split_instead hands back, a receipt or none
        self.split_receipt: ReceiptDraft | None = None

    # echo the text back
    async def log(
        self, inbound: Inbound, transport: Transport, draft: ExpenseDraft,
        source: LedgerSource = LedgerSource.TEXT
    ) -> None:
        self.calls.append("log")
        self.drafts.append((draft, source))
        await transport.send_text(inbound.user_id, inbound.text or "")

    # record the undo id
    async def undo(self, inbound: Inbound, entry_id: str, transport: Transport) -> None:
        self.calls.append("undo")
        self.undone.append(entry_id)

    # echo the image back
    async def log_receipt(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("log_receipt")
        image = await transport.fetch_media(inbound.media_ref or "")
        await transport.send_image(inbound.user_id, image, caption=inbound.text)

    # return the canned transcript
    async def transcribe(self, inbound: Inbound, transport: Transport) -> str | None:
        self.calls.append("transcribe")
        return self.heard

    # record a split of a logged receipt and hand back the canned receipt
    async def split_instead(
        self, inbound: Inbound, entry_id: str, key: str, transport: Transport
    ) -> ReceiptDraft | None:
        self.calls.append("split_instead")
        return self.split_receipt

    # record a category button
    async def on_category(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        self.calls.append("on_category")

    # record the confirmed receipt key
    async def confirm_receipt(self, inbound: Inbound, key: str, transport: Transport) -> None:
        self.calls.append("confirm_receipt")
        self.confirmed.append(key)

    # record an undo of the latest entry
    async def undo_last(self, inbound: Inbound, transport: Transport) -> None:
        self.calls.append("undo_last")
