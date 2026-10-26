# transport fake that records every call
class FakeTransport:
    # start with empty records
    def __init__(self) -> None:
        self.sent_text: list[tuple[str, str]] = []
        self.sent_buttons: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.sent_images: list[tuple[str, bytes, str | None]] = []
        self.fetched: list[str] = []
        self.media: dict[str, bytes] = {}

    # record one text
    async def send_text(self, to: str, text: str) -> None:
        self.sent_text.append((to, text))

    # record one button message
    async def send_buttons(
        self, to: str, text: str, buttons: list[tuple[str, str]]
    ) -> None:
        self.sent_buttons.append((to, text, buttons))

    # record one image
    async def send_image(self, to: str, png: bytes, caption: str | None = None) -> None:
        self.sent_images.append((to, png, caption))

    # record fetch and return canned bytes
    async def fetch_media(self, media_ref: str) -> bytes:
        self.fetched.append(media_ref)
        return self.media.get(media_ref, b"")
