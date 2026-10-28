from dataclasses import dataclass
from typing import Protocol

# when no rail can pay the person
PLAIN = "Pay them your usual way, then tap I've paid."


# how to pay one person, a line for the chat, maybe a qr image and a short label for pages
@dataclass(frozen=True)
class PayOffer:
    text: str
    qr: bytes | None = None
    label: str = ""


# one way to pay, region packs add their own
class Rail(Protocol):
    # an offer to pay this whatsapp number, none when this rail cannot
    def offer(self, payee: str, cents: int, currency: str, reference: str) -> PayOffer | None: ...


# the rails a locale turns on, the first that can pay wins, plain words otherwise
class PayRails:
    # rails in the order to try
    def __init__(self, rails: tuple[Rail, ...] = ()) -> None:
        self._rails = rails

    # the best way to pay this person
    def offer(self, payee: str, cents: int, currency: str, reference: str) -> PayOffer:
        for rail in self._rails:
            found = rail.offer(payee, cents, currency, reference)
            if found is not None:
                return found
        return PayOffer(PLAIN)
