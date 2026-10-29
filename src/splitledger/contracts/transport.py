from typing import Protocol


# outbound channel plus media fetch
class Transport(Protocol):
    # send one text message
    async def send_text(self, to: str, text: str) -> None: ...

    # send text with tappable buttons
    async def send_buttons(
        self, to: str, text: str, buttons: list[tuple[str, str]]
    ) -> None: ...

    # send text with one button that opens a link, so long urls stay out of the chat
    async def send_link(self, to: str, text: str, label: str, url: str) -> None: ...

    # send one image with optional caption
    async def send_image(self, to: str, png: bytes, caption: str | None = None) -> None: ...

    # fetch media bytes by channel reference
    async def fetch_media(self, media_ref: str) -> bytes: ...
