from dataclasses import dataclass
from typing import Literal


# one inbound message from any channel
@dataclass(frozen=True)
class Inbound:
    message_id: str
    user_id: str
    kind: Literal["text", "image", "audio", "button"]
    text: str | None = None
    media_ref: str | None = None
    button_id: str | None = None
