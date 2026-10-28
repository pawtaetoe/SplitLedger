from splitledger.contracts.messages import Inbound
from splitledger.handlers.log import ExpenseLogger, ogg_seconds
from splitledger.llm.schemas import GLOBAL
from tests.fakes import FakeLLM, FakeTransport


# minimal ogg page whose granule position says how long the clip plays
def ogg(seconds: int) -> bytes:
    return b"OggS" + bytes(2) + (seconds * 48000).to_bytes(8, "little") + bytes(20)


# one voice note from a user
def voice() -> Inbound:
    return Inbound("m1", "6581234567", "audio", media_ref="note-1")


# build a logger that never reaches the db
def logger_for(llm: FakeLLM) -> ExpenseLogger:
    return ExpenseLogger(llm, None, GLOBAL)  # type: ignore[arg-type]


# duration is read from the last ogg page and unknown formats give none
def test_ogg_seconds():
    assert ogg_seconds(ogg(12)) == 12
    assert ogg_seconds(b"not ogg") is None


# a note over a minute is refused before the model hears it
async def test_long_note_rejected():
    transport = FakeTransport()
    transport.media["note-1"] = ogg(61)
    llm = FakeLLM()
    await logger_for(llm).transcribe(voice(), transport)
    assert "60 seconds" in transport.sent_text[0][1]
    assert llm.transcribed == 0
    assert transport.sent_buttons == []


# a huge file of an unknown format is refused by size
async def test_huge_note_rejected():
    transport = FakeTransport()
    transport.media["note-1"] = b"x" * 1_000_001
    llm = FakeLLM()
    await logger_for(llm).transcribe(voice(), transport)
    assert "60 seconds" in transport.sent_text[0][1]
    assert llm.transcribed == 0


# silence asks to try again and writes nothing
async def test_silent_note_asks_again():
    transport = FakeTransport()
    transport.media["note-1"] = ogg(3)
    await logger_for(FakeLLM()).transcribe(voice(), transport)
    assert "couldn't hear" in transport.sent_text[0][1]
    assert transport.sent_buttons == []


# a clear note returns the transcript and echoes it
async def test_clear_note_returns_transcript():
    transport = FakeTransport()
    transport.media["note-1"] = ogg(3)
    llm = FakeLLM()
    llm.transcript = " coffee 4.50 "
    assert await logger_for(llm).transcribe(voice(), transport) == "coffee 4.50"
    assert transport.sent_text == [("6581234567", "*I heard*\n> coffee 4.50")]
