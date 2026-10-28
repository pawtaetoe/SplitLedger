import io
import re

import segno

from splitledger.pay.emvco import payload, tlv
from splitledger.pay.rails import PayOffer

# a singapore mobile on whatsapp, 65 then 8 or 9 and seven digits
MOBILE = re.compile(r"65([89]\d{7})")


# paynow to the payer's own mobile, one sgqr that paylah and singapore bank apps scan
class PayNow:
    # the qr payload for a fixed amount the friend cannot edit
    def code(self, mobile: str, cents: int, reference: str) -> str:
        account = (
            tlv("00", "SG.PAYNOW") + tlv("01", "0") + tlv("02", f"+65{mobile}") + tlv("03", "0")
        )
        return payload([
            ("00", "01"), ("01", "12"), ("26", account), ("52", "0000"), ("53", "702"),
            ("54", f"{cents // 100}.{cents % 100:02d}"), ("58", "SG"), ("59", "NA"),
            ("60", "Singapore"), ("62", tlv("01", reference[:25]))
        ])

    # number to type and a qr to scan, only for sgd and singapore mobiles
    def offer(self, payee: str, cents: int, currency: str, reference: str) -> PayOffer | None:
        found = MOBILE.fullmatch(payee.removeprefix("+"))
        if currency != "SGD" or found is None:
            return None
        mobile = found.group(1)
        image = io.BytesIO()
        segno.make_qr(self.code(mobile, cents, reference), error="m").save(
            image, kind="png", scale=8, border=4
        )
        text = (
            f"*PayNow* to {mobile[:4]} {mobile[4:]}, reference {reference}\n"
            "Or save the QR above and scan it from your gallery in your bank app."
        )
        return PayOffer(text, image.getvalue(), f"PayNow to {mobile[:4]} {mobile[4:]}")
