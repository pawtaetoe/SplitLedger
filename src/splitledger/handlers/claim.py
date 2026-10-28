import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import Bill, User
from splitledger.handlers.log import ensure_user, money
from splitledger.llm.schemas import ClaimMap, Locale
from splitledger.split.claims import CLOSED, BillView, ClaimBook, Line

LOG = logging.getLogger(__name__)
PROMPT = (Path(__file__).parents[1] / "llm" / "prompts" / "map_claim.md").read_text(
    encoding="utf-8"
)
# claim then a bill code then anything after it
CODE = re.compile(r"^claim\s+([a-z0-9]{4,8})\b(.*)$", re.IGNORECASE | re.DOTALL)
MODEL_TIMEOUT = 15.0
# days a payer's bill still takes claims sent without a code
RECENT_DAYS = 7
# item buttons shown at most, more would become a numbered menu that clashes with item numbers
MAX_CHOICES = 3
UNKNOWN = "*I can't find bill {}*\nCheck the code in the bill message, like _CLAIM K7Q2AB_."
NO_BILL = "*Which bill?*\nSend _CLAIM_ and the code from the bill message, like _CLAIM K7Q2AB_."
NO_MATCH = "*I couldn't match that to the bill*\nReply with item numbers, like _CLAIM {} 1,3_."
BAD_NUMBERS = "*Those numbers aren't on the bill*\nPick from 1 to {}."
CHANGE = "*Tell me again*\nLike _Anna and Ben shared the calamari, I had the pasta_."
NOTHING = "Nothing to save. Send the bill code again, like _CLAIM K7Q2AB_."


# one person's items said in words, name none is the sender
@dataclass(frozen=True)
class Draft:
    bill: Bill
    claims: list[tuple[str | None, uuid.UUID, float]]


# an item name with how much of it, for the check message
def portion_text(name: str, weight: float) -> str:
    if weight >= 1:
        return name
    return f"half of {name}" if abs(weight - 0.5) < 0.01 else f"part of {name}"


# items claimed in the chat, by code and number or in plain words with a check first
class ClaimChat:
    # sessions, the model for plain words, the claims store and the locale for new users
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], llm: LLM, book: ClaimBook,
        locale: Locale
    ) -> None:
        self._sessions = sessions
        self._llm = llm
        self._book = book
        self._locale = locale
        self._drafts: dict[str, Draft] = {}
        # the bill each user last opened, so plain words know which bill
        self._context: dict[str, str] = {}

    # claim and a code lists the items, with numbers it claims them
    async def by_code(self, inbound: Inbound, text: str, transport: Transport) -> None:
        match = CODE.match(text)
        if match is None:
            await transport.send_text(inbound.user_id, NO_BILL)
            return
        code, rest = match.group(1).upper(), match.group(2)
        bill = await self._book.open_bill(code)
        if bill is None:
            await transport.send_text(inbound.user_id, UNKNOWN.format(code))
            return
        self._context[inbound.user_id] = bill.code
        me = await self._me(inbound, bill)
        view = await self._view(bill, me)
        if bill.even_ways:
            share = money(view.share, bill.currency)
            await transport.send_text(
                inbound.user_id, f"*You're in · {share}*\nSplit evenly {bill.even_ways} ways."
            )
            return
        numbers = [int(n) for n in re.findall(r"\d+", rest)]
        if not numbers:
            await self._list(inbound, transport, view)
            return
        await self._claim_numbers(inbound, transport, view, numbers)

    # read who had what from plain words, then show everyone's share before saving
    async def free_text(self, inbound: Inbound, text: str, transport: Transport) -> None:
        bill = await self._bill_for(inbound)
        if bill is None:
            await transport.send_text(inbound.user_id, NO_BILL)
            return
        view = await self._view(bill, None)
        listing = "\n".join(
            f"{n}. {line.name}{f' x{line.qty}' if line.qty > 1 else ''}"
            for n, line in enumerate(view.lines, 1)
        )
        people = ", ".join(person.guest_name for person in view.people) or "none yet"
        prompt = (
            f"map_claim: {PROMPT}\nItems:\n{listing}\nPeople on the bill: {people}\n"
            f"Message: {text}"
        )
        try:
            result = await asyncio.wait_for(self._llm.structured(prompt, ClaimMap), MODEL_TIMEOUT)
        except Exception as error:
            LOG.warning("claim model failed with %s", type(error).__name__)
            result = ClaimMap()
        claims = self._valid(result, view.lines)
        if not claims:
            await transport.send_text(inbound.user_id, NO_MATCH.format(bill.code))
            return
        self._context[inbound.user_id] = bill.code
        self._drafts[inbound.user_id] = Draft(bill, claims)
        await self._confirm(inbound, transport, view, claims)

    # item buttons, share buttons and the check buttons
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        parts = button_id.split(":")
        if len(parts) == 4 and parts[1] in ("pick", "share") and parts[3].isdigit():
            bill = await self._book.open_bill(parts[2])
            if bill is None:
                await transport.send_text(inbound.user_id, UNKNOWN.format(parts[2]))
                return
            view = await self._view(bill, await self._me(inbound, bill))
            if parts[1] == "pick":
                await self._claim_numbers(inbound, transport, view, [int(parts[3])])
                return
            await self._share(inbound, transport, view, int(parts[3]))
            return
        draft = self._drafts.pop(inbound.user_id, None)
        if draft is None:
            await transport.send_text(inbound.user_id, NOTHING)
            return
        if parts[-1] != "ok":
            await transport.send_text(inbound.user_id, CHANGE)
            return
        await self._save(inbound, transport, draft)

    # the bill with who has each item, the free items as buttons
    async def _list(self, inbound: Inbound, transport: Transport, view: BillView) -> None:
        bill = view.bill
        lines = [
            f"{n}. {line.name} · {money(line.price, bill.currency)}"
            + (f" · {', '.join(line.holders)}" if line.holders else "")
            for n, line in enumerate(view.lines, 1)
        ]
        title = f"*{bill.vendor or 'Bill'} · code {bill.code}*"
        text = (
            f"{title}\n" + "\n".join(lines)
            + f"\n\nTap what you had, or reply with numbers like _CLAIM {bill.code} 1,3_.\n"
            "Shared something? Say it in words, like _I shared the calamari with Anna_."
        )
        free = [(n, line) for n, line in enumerate(view.lines, 1) if not line.holders]
        buttons = [(f"claim:pick:{bill.code}:{n}", f"{n}. {line.name}"[:24]) for n, line in free]
        if not buttons or len(buttons) > MAX_CHOICES:
            await transport.send_text(inbound.user_id, text)
            return
        await transport.send_buttons(inbound.user_id, text, buttons)

    # claim items by number, taken ones get a share button
    async def _claim_numbers(
        self, inbound: Inbound, transport: Transport, view: BillView, numbers: list[int]
    ) -> None:
        if view.me is None:
            return
        if any(not 1 <= n <= len(view.lines) for n in numbers):
            await transport.send_text(inbound.user_id, BAD_NUMBERS.format(len(view.lines)))
            return
        taken: list[tuple[int, Line]] = []
        for n in dict.fromkeys(numbers):
            line = view.lines[n - 1]
            error = await self._book.claim(view.bill.id, view.me.id, line.id, "mine")
            if error == CLOSED:
                await transport.send_text(inbound.user_id, CLOSED)
                return
            if error:
                taken.append((n, line))
        await self._summary(inbound, transport, view.bill, view.me.id, taken)

    # join someone on an item they already have
    async def _share(
        self, inbound: Inbound, transport: Transport, view: BillView, number: int
    ) -> None:
        if view.me is None or not 1 <= number <= len(view.lines):
            await transport.send_text(inbound.user_id, BAD_NUMBERS.format(len(view.lines)))
            return
        line = view.lines[number - 1]
        error = await self._book.claim(view.bill.id, view.me.id, line.id, "share")
        if error:
            await transport.send_text(inbound.user_id, error)
            return
        await self._summary(inbound, transport, view.bill, view.me.id, [])

    # what the user has so far and their share, with share buttons for taken items
    async def _summary(
        self, inbound: Inbound, transport: Transport, bill: Bill, me: uuid.UUID,
        taken: list[tuple[int, Line]]
    ) -> None:
        view = await self._view(bill, me)
        mine = [f"- {line.name}" for line in view.lines if line.mine] or ["Nothing yet"]
        text = f"*Your share so far · {money(view.share, bill.currency)}*\n" + "\n".join(mine)
        if not taken:
            await transport.send_text(inbound.user_id, text)
            return
        notes = [f"{line.name} is taken by {line.holders[0]}" for _, line in taken]
        buttons = [
            (f"claim:share:{bill.code}:{n}", f"I shared {line.name}"[:20]) for n, line in taken[:3]
        ]
        await transport.send_buttons(inbound.user_id, text + "\n\n" + "\n".join(notes), buttons)

    # everyone's items and share from the words, saved only after Looks right
    async def _confirm(
        self, inbound: Inbound, transport: Transport, view: BillView,
        claims: list[tuple[str | None, uuid.UUID, float]]
    ) -> None:
        bill = view.bill
        me = await self._me(inbound, bill)
        ids = await self._ids(view, me, claims)
        shares = await self._book.preview(
            bill, [(ids[name], item, weight) for name, item, weight in claims]
        )
        names = {line.id: line.name for line in view.lines}
        lines = []
        for name in dict.fromkeys(name for name, _, _ in claims):
            items = [portion_text(names[item], w) for who, item, w in claims if who == name]
            share = money(shares[ids[name]], bill.currency)
            lines.append(f"- {name or 'You'}: {', '.join(items)} · *{share}*")
        text = "*Check this*\n" + "\n".join(lines) + "\n\n_Service and tax are included._"
        await transport.send_buttons(
            inbound.user_id, text, [("claim:ok", "Looks right"), ("claim:no", "Change")]
        )

    # save the checked claims, adding friends named for the first time
    async def _save(self, inbound: Inbound, transport: Transport, draft: Draft) -> None:
        bill = draft.bill
        me = await self._me(inbound, bill)
        claims: list[tuple[uuid.UUID, uuid.UUID, float]] = []
        for name, item, weight in draft.claims:
            person = me if name is None else await self._book.join(bill.id, name)
            claims.append((person, item, weight))
        if not await self._book.apply(bill.id, claims):
            await transport.send_text(inbound.user_id, CLOSED)
            return
        view = await self._view(bill, me)
        await transport.send_text(
            inbound.user_id,
            f"*Saved*\nYour share so far: *{money(view.share, bill.currency)}*\n"
            "Everyone can check theirs on the claim page."
        )

    # person ids for the names, existing people by name and stand ins for new ones
    async def _ids(
        self, view: BillView, me: uuid.UUID, claims: list[tuple[str | None, uuid.UUID, float]]
    ) -> dict[str | None, uuid.UUID]:
        known = {person.guest_name.lower(): person.id for person in view.people}
        ids: dict[str | None, uuid.UUID] = {None: me}
        for name, _, _ in claims:
            if name is not None and name not in ids:
                ids[name] = known.get(name.lower(), uuid.uuid4())
        return ids

    # valid claims from the model, numbers on the bill and portions between 0 and 1
    def _valid(
        self, result: ClaimMap, lines: list[Line]
    ) -> list[tuple[str | None, uuid.UUID, float]]:
        claims: dict[tuple[str | None, uuid.UUID], float] = {}
        for person in result.people:
            name = " ".join((person.name or "").split()) or None
            for part in person.items:
                if not 1 <= part.item <= len(lines):
                    continue
                portion = part.portion if part.portion and 0 < part.portion <= 1 else 1.0
                claims[(name, lines[part.item - 1].id)] = portion
        return [(name, item, weight) for (name, item), weight in claims.items()]

    # the bill as one person sees it
    async def _view(self, bill: Bill, me: uuid.UUID | None) -> BillView:
        view = await self._book.view(bill.code, me)
        if view is None:
            raise ValueError("bill vanished")
        return view

    # the sender's person on the bill, their profile name kept for friends to see
    async def _me(self, inbound: Inbound, bill: Bill) -> uuid.UUID:
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            user = await session.get(User, user_id)
            if user is not None and not user.display_name and inbound.name:
                user.display_name = inbound.name
            name = (user.display_name if user else None) or f"Friend {inbound.user_id[-4:]}"
            await session.commit()
        return await self._book.person_for(bill.id, user_id, name)

    # the bill last opened in this chat, else the sender's latest bill as payer
    async def _bill_for(self, inbound: Inbound) -> Bill | None:
        code = self._context.get(inbound.user_id)
        bill = await self._book.open_bill(code) if code else None
        if bill is not None:
            return bill
        async with self._sessions() as session:
            query = select(User.id).where(User.wa_user_id == inbound.user_id)
            user_id = (await session.execute(query)).scalar_one_or_none()
        if user_id is None:
            return None
        since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
        return await self._book.latest_paid_by(user_id, since)
