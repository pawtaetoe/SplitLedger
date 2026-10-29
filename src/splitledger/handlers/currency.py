import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import User
from splitledger.llm.schemas import CURRENCY_CODES, by_country_code

# a phone's country calling code and that country's currency, longest code wins
CALLING_CODES = {
    "1": "USD", "7": "RUB", "20": "EGP", "27": "ZAR", "30": "EUR", "31": "EUR", "32": "EUR",
    "33": "EUR", "34": "EUR", "36": "HUF", "39": "EUR", "40": "RON", "41": "CHF", "43": "EUR",
    "44": "GBP", "45": "DKK", "46": "SEK", "47": "NOK", "48": "PLN", "49": "EUR", "52": "MXN",
    "54": "ARS", "55": "BRL", "56": "CLP", "57": "COP", "60": "MYR", "61": "AUD", "62": "IDR",
    "63": "PHP", "64": "NZD", "65": "SGD", "66": "THB", "81": "JPY", "82": "KRW", "84": "VND",
    "86": "CNY", "90": "TRY", "91": "INR", "92": "PKR", "94": "LKR", "95": "MMK", "351": "EUR",
    "352": "EUR", "353": "EUR", "354": "ISK", "356": "EUR", "357": "EUR", "358": "EUR",
    "370": "EUR", "371": "EUR", "372": "EUR", "385": "EUR", "386": "EUR", "420": "CZK",
    "421": "EUR", "673": "BND", "852": "HKD", "853": "MOP", "855": "KHR", "856": "LAK",
    "880": "BDT", "886": "TWD", "960": "MVR", "966": "SAR", "971": "AED", "972": "ILS",
    "974": "QAR"
}
# the main timezone for a calling code, bigger countries get their most used one
TIMEZONES = {
    "1": "America/New_York", "7": "Europe/Moscow", "27": "Africa/Johannesburg",
    "31": "Europe/Amsterdam", "33": "Europe/Paris", "34": "Europe/Madrid", "39": "Europe/Rome",
    "41": "Europe/Zurich", "44": "Europe/London", "49": "Europe/Berlin",
    "52": "America/Mexico_City", "55": "America/Sao_Paulo", "60": "Asia/Kuala_Lumpur",
    "61": "Australia/Sydney",
    "62": "Asia/Jakarta", "63": "Asia/Manila", "64": "Pacific/Auckland", "65": "Asia/Singapore",
    "66": "Asia/Bangkok", "81": "Asia/Tokyo", "82": "Asia/Seoul", "84": "Asia/Ho_Chi_Minh",
    "86": "Asia/Shanghai", "91": "Asia/Kolkata", "673": "Asia/Brunei", "852": "Asia/Hong_Kong",
    "853": "Asia/Macau", "855": "Asia/Phnom_Penh", "886": "Asia/Taipei", "971": "Asia/Dubai"
}
# names for the currencies offered as buttons
NAMES = {
    "SGD": "Singapore dollar", "MYR": "Malaysian ringgit", "USD": "US dollar", "EUR": "Euro",
    "GBP": "British pound", "AUD": "Australian dollar", "JPY": "Japanese yen",
    "INR": "Indian rupee", "IDR": "Indonesian rupiah", "THB": "Thai baht",
    "PHP": "Philippine peso", "HKD": "Hong Kong dollar", "CNY": "Chinese yuan",
    "KRW": "Korean won", "VND": "Vietnamese dong", "TWD": "Taiwan dollar",
    "NZD": "New Zealand dollar", "CHF": "Swiss franc", "AED": "UAE dirham"
}
# offered after the user's own currencies, a whatsapp list holds ten
COMMON = ("SGD", "USD", "EUR", "GBP", "MYR", "AUD", "JPY", "INR", "IDR", "THB")
KNOWN = frozenset({code.upper() for code in CURRENCY_CODES} | set(CALLING_CODES.values()))
# a typed change like currency jpy or main currency usd
TYPED = re.compile(r"^(?:base |main |home )?currency\s+(?:to\s+)?([a-z]{3})$", re.IGNORECASE)
ASK = (
    "*Your main currency is {}*\n"
    "Totals, plans and the dashboard use it, and other currencies are converted at "
    "today's rate. Pick one, or reply like _currency JPY_."
)
DONE = (
    "✅ *Main currency is now {}*\n"
    "Spending you log without a currency is in {}. If you have a plan, its amounts "
    "stay the same, so check them in Budget plan."
)
UNKNOWN = "*I don't know {}*\nUse the three letter code, like _currency JPY_."


# the currency of the country a whatsapp number is from, else the fallback
def currency_for(wa_user_id: str, fallback: str) -> str:
    return by_country_code(wa_user_id, CALLING_CODES, fallback)


# the main timezone of the country a whatsapp number is from, else the fallback
def timezone_for(wa_user_id: str, fallback: str) -> str:
    return by_country_code(wa_user_id, TIMEZONES, fallback)


# shows and changes the user's main currency
class CurrencyChoice:
    # sessions for users
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # the current currency and up to ten to pick from, the phone's own near the top
    async def ask(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            current = await self._current(session, inbound.user_id)
        phone = currency_for(inbound.user_id, current)
        codes = list(dict.fromkeys([current, phone, *COMMON]))[:10]
        buttons = [
            (f"cur:{code}", f"{code} — {NAMES[code]}" if code in NAMES else code)
            for code in codes
        ]
        await transport.send_buttons(inbound.user_id, ASK.format(current), buttons)

    # a tap on one currency
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        await self.change(inbound, button_id.removeprefix("cur:"), transport)

    # a typed change, false when the text isn't one
    async def typed(self, inbound: Inbound, text: str, transport: Transport) -> bool:
        found = TYPED.match(text.strip())
        if found is None:
            return False
        await self.change(inbound, found.group(1), transport)
        return True

    # save the new main currency
    async def change(self, inbound: Inbound, code: str, transport: Transport) -> None:
        code = code.upper()
        if code not in KNOWN:
            await transport.send_text(inbound.user_id, UNKNOWN.format(code))
            return
        async with self._sessions() as session:
            user = (await session.execute(
                select(User).where(User.wa_user_id == inbound.user_id)
            )).scalar_one_or_none()
            if user is None:
                return
            user.currency = code
            await session.commit()
        await transport.send_text(inbound.user_id, DONE.format(code, code))

    # the user's currency now
    async def _current(self, session: AsyncSession, wa_user_id: str) -> str:
        found = await session.execute(select(User.currency).where(User.wa_user_id == wa_user_id))
        return found.scalar_one_or_none() or "USD"
