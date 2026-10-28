# emvco merchant qr payloads, shared by paynow, duitnow, promptpay and pix


# one tag, its two digit length and the value
def tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


# crc16 ccitt false as four hex digits, the checksum emvco asks for
def crc16(data: str) -> str:
    crc = 0xFFFF
    for byte in data.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return f"{crc:04X}"


# the whole payload from ordered fields with the checksum field on the end
def payload(fields: list[tuple[str, str]]) -> str:
    body = "".join(tlv(tag, value) for tag, value in fields) + "6304"
    return body + crc16(body)
