import asyncio

from tests.fakes import FakeTransport


# fake transport records every send
def test_fake_transport_records_calls():
    async def run():
        fake = FakeTransport()
        await fake.send_text("alice", "hi")
        await fake.send_buttons("alice", "pick", [("save", "Save")])
        await fake.send_image("alice", b"png", caption="receipt")
        data = await fake.fetch_media("media-1")
        assert data == b""
        assert fake.sent_text == [("alice", "hi")]
        assert fake.sent_buttons == [("alice", "pick", [("save", "Save")])]
        assert fake.sent_images == [("alice", b"png", "receipt")]
        assert fake.fetched == ["media-1"]

    asyncio.run(run())
