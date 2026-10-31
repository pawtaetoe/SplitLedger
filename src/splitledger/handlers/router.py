import asyncio
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Literal

from splitledger.contracts.llm import LLM
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerSource
from splitledger.db.session import SessionFactory
from splitledger.handlers.ask import SpendAssistant
from splitledger.handlers.bill import GroupBills
from splitledger.handlers.bills import UpcomingBills
from splitledger.handlers.claim import ClaimChat
from splitledger.handlers.currency import CurrencyChoice
from splitledger.handlers.dashboard import LINK_MINUTES, DashboardLink
from splitledger.handlers.log import ExpenseLogger, quick_parse
from splitledger.handlers.plan import PlanManager
from splitledger.handlers.settle import SettleUp
from splitledger.llm.client import MetaLLM
from splitledger.llm.schemas import GLOBAL, ExpenseDraft, Locale, MessageResult, Regions
from splitledger.llm.schemas_sg import SG
from splitledger.pay.rails import PayRails
from splitledger.pay.rails_sg import PayNow
from splitledger.plans.afford import CORE_RULES, AffordPack
from splitledger.plans.afford_sg import SG_FIELDS, SG_RULES
from splitledger.plans.quota import QuotaBook
from splitledger.settings import settings
from splitledger.split.claims import ClaimBook
from splitledger.split.fx import Rates
from splitledger.split.tokens import ClaimTokens, LoginLinks

LOG = logging.getLogger(__name__)
# add a region with a new schemas module and list it here
LOCALES = {"global": GLOBAL, "sg": SG}
AFFORD = {"global": AffordPack(CORE_RULES), "sg": AffordPack([*CORE_RULES, *SG_RULES], SG_FIELDS)}
# region packs picked by a number's country calling code, other numbers get LOCALE
PACKS = {"65": "sg"}
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
# the payer closing their bill in words
CLOSE = re.compile(r"^(close|finish|finalise|finalize)\s+(the\s+|my\s+)?bill\b", re.IGNORECASE)
# item numbers like 2, 6 or 1 and 3
NUMBER_LIST = re.compile(r"^\d{1,2}(?:\s*(?:,|&|and|\s)\s*\d{1,2})*$", re.IGNORECASE)
# taking items back off a claim, like remove 2 or not 3 and 5
UNCLAIM = re.compile(
    r"^(?:remove|drop|unclaim|not)\s+\d{1,2}(?:\s*(?:,|&|and|\s)\s*\d{1,2})*$", re.IGNORECASE
)
# a caption longer than this may say who had what
CAPTION_WORDS = 3
# a head count in a split like 4 ways or 4 pax
WAYS = re.compile(r"\b(\d{1,2})\s*(?:ways?|pax|people|persons?)\b", re.IGNORECASE)
SplitMode = Literal["even", "items", "unsure"]
# words that mean a split by what each person had, not an even one
BY_ITEM = frozenset({"dish", "dishes", "item", "items", "own", "had"})
# words that mark a bill they expect rather than one they paid
REPEAT_WORDS = frozenset({"every", "monthly", "weekly", "yearly", "upcoming"})
HELP = "*What would you like to do?*\nTap one below."
UNCLEAR = "*Sorry, I didn't catch that*\nTap one below, or type something like _coffee 4.50_."
STALE = "*That button no longer works*\nTap one below instead."
UNREADABLE = (
    "*I can't read that kind of message*\nSend text, a photo or a voice note, or tap one below."
)
NO_FILES = (
    "*I can't open files yet*\n"
    "Send a photo or screenshot of the receipt instead, or tap one below."
)
# the menu shown wherever the user might not know what to do next
MENU = [
    ("menu:log", "Log spending — type it, or send a photo or voice note"),
    ("menu:ask", "Ask about my spending — totals, top buys, can I afford it"),
    ("menu:split", "Split a bill — share a receipt with friends"),
    ("menu:bills", "Upcoming bills — see, add or get reminders"),
    ("menu:plan", "Budget plan — set one up or see how you're doing"),
    ("menu:dash", "My dashboard — who owes whom, shared bills and this month"),
    ("menu:currency", "Main currency — what totals and plans use"),
    ("menu:undo", "Undo last entry — remove what you just saved")
]
SPLIT_HINT = (
    "*Split a bill*\nSend the receipt photo with the caption _split_ to share by item,\n"
    "or _split 4 ways_ to share it evenly. I'll make a message to forward to your group."
)
HINTS = {
    "menu:split": SPLIT_HINT,
    "menu:log": "*Log spending*\nType what you bought, like _coffee 4.50_.\n"
                "Or send a receipt photo or a voice note.",
    "menu:ask": "*Ask in your own words*\n- _how much on food this month?_\n"
                "- _what were my biggest buys last week?_\n- _can I afford a 300 hotel?_"
}
# typed words that send the dashboard link without asking the model
DASH_WORDS = frozenset({"dashboard", "overview", "summary"})
# typed words that open the menu without asking the model
MENU_WORDS = frozenset({"menu", "help", "options"})
# typed words that offer to remove the last entry without asking the model
UNDO_WORDS = frozenset({"undo", "oops", "delete", "remove"})


# lowercase words in a message
def words_in(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


# a friend arriving from a forwarded bill, who needs the bill and not the welcome
def claiming(inbound: Inbound) -> bool:
    if inbound.kind == "button":
        return (inbound.button_id or "").startswith("claim:")
    return inbound.kind == "text" and CLAIM.match((inbound.text or "").strip()) is not None


# reads free text with one model call, keyword rules only when the model fails
class MessageReader:
    # model for reading and the region, or regions by phone number, for the fallback rules
    def __init__(self, llm: LLM, locale: Locale | Regions) -> None:
        self._llm = llm
        self._regions = locale if isinstance(locale, Regions) else Regions(locale)

    # intent and expense from the model, rules when it is down, slow or returns junk
    async def read(self, text: str, user: str = "") -> MessageResult:
        try:
            return await asyncio.wait_for(
                self._llm.structured(f"read_message: {PROMPT}{text}", MessageResult),
                MODEL_TIMEOUT
            )
        except Exception as error:
            LOG.warning("message model failed with %s, using rules", type(error).__name__)
            return self.by_rules(text, user)

    # split word, question shape, then a loggable amount in the user's region words, else unclear
    def by_rules(self, text: str, user: str = "") -> MessageResult:
        words = words_in(text)
        if "split" in words:
            by_item = bool(BY_ITEM.intersection(words))
            ways = None if by_item else WAYS.search(text)
            mode: SplitMode = "even" if ways and not by_item else "items"
            return MessageResult(
                intent="split", ways=int(ways.group(1)) if ways else None, split_mode=mode
            )
        if text.rstrip().endswith("?") or (words and words[0] in QUESTION_WORDS):
            return MessageResult(intent="ask")
        if REPEAT_WORDS.intersection(words):
            return MessageResult(intent="upcoming")
        draft = quick_parse(text, self._regions.for_user(user))
        if draft is not None:
            return MessageResult(intent="log", expense=draft)
        return MessageResult(intent="unclear")


# routes one inbound message, fixed rules for bot formats and the model for language
class Router:
    # logger for saving, reader for free text, assistant for questions, plans for budgets
    # bills for what the user expects to pay, group for shared bills, claims for who had what
    # and dash for the dashboard link
    def __init__(
        self, logger: ExpenseLogger, reader: MessageReader, assistant: SpendAssistant,
        plans: PlanManager, bills: UpcomingBills, group: GroupBills, claims: ClaimChat,
        dash: DashboardLink, currency: CurrencyChoice
    ) -> None:
        self._logger = logger
        self._reader = reader
        self._assistant = assistant
        self._plans = plans
        self._bills = bills
        self._group = group
        self._claims = claims
        self._dash = dash
        self._currency = currency

    # greet new users unless they came to claim a bill, then pick the handler
    async def handle(self, inbound: Inbound, transport: Transport) -> None:
        if not claiming(inbound):
            await self._plans.welcome_if_new(inbound, transport)
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
        # stickers, files and anything else unreadable get what the bot can read
        await self.menu(inbound, transport, NO_FILES if inbound.text == "document" else UNREADABLE)

    # each button prefix goes to its handler, unknown ones echo
    async def _button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        if button_id == "plan:web":
            await self._dash.send(inbound, transport, "plan")
            return
        if button_id.startswith("plan:"):
            await self._plans.on_button(inbound, button_id, transport)
            return
        if button_id.startswith("bills:"):
            await self._bills.on_button(inbound, button_id, transport)
            return
        if button_id == "owe:all":
            await self._assistant.owed(inbound, transport)
            return
        if button_id.startswith(("bill:", "pay:")):
            await self._group.on_button(inbound, button_id, transport)
            return
        if button_id.startswith("claim:"):
            await self._claims.on_button(inbound, button_id, transport)
            return
        if button_id.startswith("menu:"):
            await self._menu_choice(inbound, button_id, transport)
            return
        if button_id.startswith("cur:"):
            await self._currency.on_button(inbound, button_id, transport)
            return
        if button_id.startswith("undo:"):
            await self._logger.undo(inbound, button_id.removeprefix("undo:"), transport)
            return
        if button_id.startswith("cat:"):
            await self._logger.on_category(inbound, button_id, transport)
            return
        if button_id.startswith("split:"):
            entry_id, _, key = button_id.removeprefix("split:").partition(":")
            receipt = await self._logger.split_instead(inbound, entry_id, key, transport)
            if receipt is not None:
                # no caption said how, so ask by item or evenly
                await self._group.from_draft(inbound, transport, receipt, unsure=True)
            return
        if button_id.startswith("receipt:"):
            key = button_id.removeprefix("receipt:")
            await self._logger.confirm_receipt(inbound, key, transport)
            return
        # a button from an older version of the bot
        await self.menu(inbound, transport, STALE)

    # a photo is a receipt unless its caption asks to split
    async def _image(self, inbound: Inbound, transport: Transport) -> None:
        caption = (inbound.text or "").strip()
        result = await self._reader.read(caption, inbound.user_id) if caption else None
        if result is not None and result.intent == "split":
            unsure = result.split_mode == "unsure"
            bill = await self._group.from_receipt(inbound, transport, result.ways, unsure)
            # a caption that says who had what is read as claims on the new bill
            if bill is not None and not result.ways and len(words_in(caption)) > CAPTION_WORDS:
                await self._claims.free_text(inbound, caption, transport, bill.code, quiet=True)
            return
        await self._logger.log_receipt(inbound, transport)

    # commands and claim codes by rule, everything else read by the model
    async def _text(
        self, inbound: Inbound, text: str, transport: Transport, source: LedgerSource
    ) -> None:
        if text.startswith("/"):
            command = text.split()[0].lower()
            if command == "/undo":
                await self._logger.undo_last(inbound, transport)
                return
            if command == "/plan":
                await self._plans.show(inbound, transport)
                return
            await self.menu(inbound, transport)
            return
        if await self._plans.waiting_for_text(inbound.user_id):
            await self._plans.on_text(inbound, transport)
            return
        if words_in(text) and set(words_in(text)) <= DASH_WORDS | {"my", "open", "the"}:
            await self._dash.send(inbound, transport)
            return
        if words_in(text) and set(words_in(text)) <= MENU_WORDS:
            await self.menu(inbound, transport)
            return
        if UNCLAIM.match(text) and await self._claims.has_bill(inbound.user_id):
            await self._claims.unclaim(inbound, text, transport)
            return
        said = set(words_in(text))
        if said & UNDO_WORDS and said <= UNDO_WORDS | {"last", "that", "it", "entry"}:
            await self._logger.undo_last(inbound, transport)
            return
        if await self._currency.typed(inbound, text, transport):
            return
        if CLOSE.match(text):
            await self._group.close_latest(inbound, transport)
            return
        if CLAIM.match(text):
            await self._claims.by_code(inbound, text, transport)
            return
        if NUMBER_LIST.match(text) and await self._claims.has_bill(inbound.user_id):
            await self._claims.numbers(inbound, text, transport)
            return
        result = await self._reader.read(text, inbound.user_id)
        if result.intent == "log":
            draft = result.expense or ExpenseDraft(amount_cents=None)
            await self._logger.log(inbound, transport, draft, source)
            return
        if result.intent in ("ask", "upcoming"):
            await self._assistant.answer(inbound, transport)
            return
        if result.intent == "plan":
            await self._plans.show(inbound, transport)
            return
        if result.intent == "chat":
            await self.menu(inbound, transport)
            return
        if result.intent == "split" and result.ways:
            await self._group.split_evenly(inbound, transport, result.ways)
            return
        if result.intent == "split":
            await transport.send_text(inbound.user_id, SPLIT_HINT)
            return
        if result.intent == "claim":
            await self._claims.free_text(inbound, text, transport)
            return
        await self.menu(inbound, transport, UNCLEAR)

    # what the user can do, as choices to tap rather than commands to remember
    async def menu(self, inbound: Inbound, transport: Transport, text: str = HELP) -> None:
        await transport.send_buttons(inbound.user_id, text, MENU)

    # one menu choice, examples to copy for free text and the handler for the rest
    async def _menu_choice(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        if button_id in HINTS:
            await transport.send_text(inbound.user_id, HINTS[button_id])
            return
        if button_id == "menu:bills":
            await self._bills.show(inbound, transport)
            return
        if button_id == "menu:plan":
            await self._plans.show(inbound, transport)
            return
        if button_id == "menu:currency":
            await self._currency.ask(inbound, transport)
            return
        if button_id == "menu:dash":
            await self._dash.send(inbound, transport)
            return
        if button_id == "menu:undo":
            await self._logger.undo_last(inbound, transport)
            return
        await self.menu(inbound, transport)


llm = MetaLLM()
locale = LOCALES[settings.locale]
regions = Regions(locale, {code: LOCALES[key] for code, key in PACKS.items()})
# one set of exchange rates for plans and the dashboard, fetched twice a day at most
rates = Rates(settings.exchange_rate_api_key)
book = QuotaBook(SessionFactory, rates)
logger = ExpenseLogger(llm, SessionFactory, locale, book)
bills = UpcomingBills(SessionFactory, logger, settings.remind_hour)
# the bot number friends message to claim, from either provider
bot_number = settings.whatsapp_bot_number or re.sub(r"\D", "", settings.twilio_whatsapp_from)
# pay rails and settling up, shared by the chat and the dashboard, paynow only offers itself
# to singapore numbers in sgd, so it is on for everyone
pay_rails = PayRails((PayNow(),))
settle = SettleUp(SessionFactory, pay_rails)
group = GroupBills(
    SessionFactory, logger, locale, ClaimTokens(settings.app_secret), settings.public_base_url,
    bot_number, settle
)
links = DashboardLink(
    SessionFactory, LoginLinks(ClaimTokens(settings.app_secret), LINK_MINUTES * 60),
    settings.public_base_url, locale
)
message_router = Router(
    logger,
    MessageReader(llm, regions),
    SpendAssistant(
        llm, SessionFactory, book, AFFORD[settings.locale],
        {code: AFFORD[key] for code, key in PACKS.items()}
    ),
    PlanManager(SessionFactory, book, locale, llm, links),
    bills,
    group,
    ClaimChat(SessionFactory, llm, ClaimBook(SessionFactory), locale, links),
    links,
    CurrencyChoice(SessionFactory)
)
