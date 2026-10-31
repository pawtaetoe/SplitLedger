from dataclasses import dataclass
from typing import Literal


# one inbound message from any channel
@dataclass(frozen=True)
class Inbound:
    message_id: str
    user_id: str
    # other is a message the bot cannot read, its text names the type like document
    kind: Literal["text", "image", "audio", "button", "other"]
    text: str | None = None
    media_ref: str | None = None
    button_id: str | None = None
    # the sender's whatsapp profile name when the channel gives it
    name: str | None = None
