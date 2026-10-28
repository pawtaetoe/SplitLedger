from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.handlers.log import ensure_user
from splitledger.llm.schemas import Locale
from splitledger.split.tokens import LoginLinks

# minutes a sign in link from the chat works
LINK_MINUTES = 15
LINK = (
    "*Your dashboard*\n{}\n\n"
    "Who owes whom, open bills, this month and what's coming up.\n"
    "_The link works for 15 minutes. Please don't forward it._"
)


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

    # one link for this user
    async def send(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            await session.commit()
        token = self._links.make(str(user_id), datetime.now(UTC))
        await transport.send_text(inbound.user_id, LINK.format(f"{self._base_url}/d/{token}"))
