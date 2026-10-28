import asyncio
import logging
import re
from dataclasses import replace
from pathlib import Path

from splitledger.contracts.llm import LLM
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerSource
from splitledger.db.session import SessionFactory
from splitledger.handlers.log import ExpenseLogger, quick_parse
from splitledger.llm.client import MetaLLM
from splitledger.llm.schemas import GLOBAL, ExpenseDraft, Locale, MessageResult
from splitledger.llm.schemas_sg import SG
from splitledger.settings import settings

LOG = logging.getLogger(__name__)
# add a region with a new schemas module and list it here
LOCALES = {"global": GLOBAL, "sg": SG}
PROMPTS = Path(__file__).parents[1] / "llm" / "prompts"
PROMPT = "".join(
    (PROMPTS / name).read_text(encoding="utf-8") for name in ("classify.md", "parse_expense.md")
)
# seconds to wait for the model before falling back to rules
MODEL_TIMEOUT = 8.0
# the claim format the bot itself hands out
CLAIM = re.compile(r"^claim\s+[a-z0-9]{4,8}\b", re.IGNORECASE)
QUESTION_WORDS = frozenset(
    {"how", "what", "when", "where", "which", "who", "why", "am", "is", "are", "did", "do",
     "does", "show", "list"}
)
HELP = (
    "I log your spending and split bills.\n"
    "Type it: coffee 4.50\n"
    "Or send a receipt photo or a voice note.\n"
    "Tap Undo, or send /undo to remove your last entry.\n"
    "Send /help to see this again."
)
UNCLEAR = "Sorry, I didn't catch that. Try something like coffee 4.50, or send /help"
# replies for intents whose handlers land in later tasks
SOON = {
    "ask": "Spending questions are coming soon",
    "split": "Group bill splitting is coming soon",
    "claim": "Claiming items on a bill is coming soon"
}


# lowercase words in a message
def words_in(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


# reads free text with one model call, keyword rules only when the model fails
class MessageReader:
    # model for reading and the locale for the fallback rules
    def __init__(self, llm: LLM, locale: Locale) -> None:
        self._llm = llm
        self._locale = locale

    # intent and expense from the model, rules when it is down, slow or returns junk
    async def read(self, text: str) -> MessageResult:
        try:
            return await asyncio.wait_for(
                self._llm.structured(f"read_message: {PROMPT}{text}", MessageResult),
                MODEL_TIMEOUT
            )
        except Exception as error:
            LOG.warning("message model failed with %s, using rules", type(error).__name__)
            return self.by_rules(text)

    # split word, question shape, then a loggable amount, else unclear
    def by_rules(self, text: str) -> MessageResult:
        words = words_in(text)
        if "split" in words:
            return MessageResult(intent="split")
        if text.rstrip().endswith("?") or (words and words[0] in QUESTION_WORDS):
            return MessageResult(intent="ask")
        draft = quick_parse(text, self._locale)
        if draft is not None:
            return MessageResult(intent="log", expense=draft)
        return MessageResult(intent="unclear")


# routes one inbound message, fixed rules for bot formats and the model for language
class Router:
    # expense logger for logging and its buttons, reader for free text
    def __init__(self, logger: ExpenseLogger, reader: MessageReader) -> None:
        self._logger = logger
        self._reader = reader

    # pick the handler for this message
    async def handle(self, inbound: Inbound, transport: Transport) -> None:
        if inbound.kind == "button":
            await self._button(inbound, inbound.button_id or "", transport)
            return
        if inbound.kind == "image" and inbound.media_ref:
            await self._image(inbound, transport)
            return
        if inbound.kind == "audio" and inbound.media_ref:
            heard = await self._logger.transcribe(inbound, transport)
            if heard:
                voice = replace(inbound, kind="text", text=heard)
                await self._text(voice, heard, transport, LedgerSource.VOICE)
            return
        if inbound.kind == "text":
            text = (inbound.text or "").strip()
            await self._text(inbound, text, transport, LedgerSource.TEXT)
            return
        if inbound.kind == "audio":
            await transport.send_text(inbound.user_id, "[audio]")
            return
        await transport.send_text(inbound.user_id, inbound.text or "")

    # undo and receipt buttons go to the logger, others echo
    async def _button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        if button_id.startswith("undo:"):
            await self._logger.undo(inbound, button_id.removeprefix("undo:"), transport)
            return
        if button_id.startswith("receipt:"):
            key = button_id.removeprefix("receipt:")
            await self._logger.confirm_receipt(inbound, key, transport)
            return
        await transport.send_text(inbound.user_id, button_id)

    # a photo is a receipt unless its caption asks to split
    async def _image(self, inbound: Inbound, transport: Transport) -> None:
        caption = (inbound.text or "").strip()
        if caption and (await self._reader.read(caption)).intent == "split":
            await transport.send_text(inbound.user_id, SOON["split"])
            return
        await self._logger.log_receipt(inbound, transport)

    # commands and claim codes by rule, everything else read by the model
    async def _text(
        self, inbound: Inbound, text: str, transport: Transport, source: LedgerSource
    ) -> None:
        if text.startswith("/"):
            if text.split()[0].lower() == "/undo":
                await self._logger.undo_last(inbound, transport)
                return
            await transport.send_text(inbound.user_id, HELP)
            return
        if CLAIM.match(text):
            await transport.send_text(inbound.user_id, SOON["claim"])
            return
        result = await self._reader.read(text)
        if result.intent == "log":
            draft = result.expense or ExpenseDraft(amount_cents=None)
            await self._logger.log(inbound, transport, draft, source)
            return
        if result.intent == "chat":
            await transport.send_text(inbound.user_id, HELP)
            return
        await transport.send_text(inbound.user_id, SOON.get(result.intent, UNCLEAR))


llm = MetaLLM()
locale = LOCALES[settings.locale]
message_router = Router(ExpenseLogger(llm, SessionFactory, locale), MessageReader(llm, locale))
