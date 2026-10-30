import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.handlers.log import ensure_user
from splitledger.llm.schemas import Locale
from splitledger.split.tokens import ClaimTokens, LoginLinks

# minutes a sign in link from the chat works
LINK_MINUTES = 15
# the cookie that keeps a phone signed in, and for how long
COOKIE = "sl_me"
COOKIE_DAYS = 30
BILL_DAYS = 14
BILL_LINK = (
    "*Your page for this bill*\n"
    "Tick what you had. It saves as you go, and your share goes into your own records.\n"
    "_The button is yours and works for 14 days. Please don't forward it._"
)
WARNING = "_The button works for 15 minutes. Please don't forward it._"
# the label on buttons that open the dashboard
SEE_ALL = "See it all"
# what each page is called, what it is for and its button
PAGES = {
    "me": (
        "Your dashboard", "Who owes whom, shared bills, this month and upcoming bills.",
        "Open dashboard"
    ),
    "plan": (
        "Set up your plan", "Every choice on one screen, with the amounts as you go.",
        "Open the plan"
    )
}


# links that open one bill as one person for two weeks, and nothing else
def bill_links(tokens: ClaimTokens, code: str) -> LoginLinks:
    return LoginLinks(tokens, BILL_DAYS * 86400, f"bill-{code.upper()}")


# a short lived link that signs this user in and opens their dashboard
def dashboard_url(base_url: str, links: LoginLinks, user_id: uuid.UUID) -> str:
    return f"{base_url.rstrip('/')}/d/{links.make(str(user_id), datetime.now(UTC))}"


# sends a short lived sign in link to the user's own dashboard
class DashboardLink:
    # sessions, the link signer and where the web app lives
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], links: LoginLinks, base_url: str,
        locale: Locale
    ) -> None:
        self._sessions = sessions
        self._links = links
        self._base_url = base_url.rstrip("/")
        self._locale = locale

    # one link for this user to the dashboard or another page
    async def send(self, inbound: Inbound, transport: Transport, page: str = "me") -> None:
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            await session.commit()
        token = self._links.make(str(user_id), datetime.now(UTC))
        title, what, label = PAGES[page]
        url = f"{self._base_url}/d/{token}" + ("" if page == "me" else f"?to={page}")
        await transport.send_link(inbound.user_id, f"*{title}*\n{what}\n{WARNING}", label, url)

    # a short lived link to this user's dashboard
    def url(self, user_id: uuid.UUID) -> str:
        return dashboard_url(self._base_url, self._links, user_id)

    # a personal link that opens one bill as this user for two weeks
    async def bill_url(self, inbound: Inbound, code: str) -> str:
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            await session.commit()
        links = bill_links(self._links.tokens, code)
        return f"{self._base_url}/b/{code}/{links.make(str(user_id), datetime.now(UTC))}"

    # the personal bill link on its own
    async def send_bill(self, inbound: Inbound, transport: Transport, code: str) -> None:
        url = await self.bill_url(inbound, code)
        await transport.send_link(inbound.user_id, BILL_LINK, "Open the bill", url)
