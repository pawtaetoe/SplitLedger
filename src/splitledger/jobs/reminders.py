import asyncio
import logging
from datetime import UTC, datetime

from splitledger.contracts.transport import Transport
from splitledger.handlers.bills import UpcomingBills

LOG = logging.getLogger(__name__)
# seconds between checks for bills to remind about
CHECK_SECONDS = 600


# check for due reminders every few minutes while the app runs
async def reminder_loop(bills: UpcomingBills, transport: Transport) -> None:
    while True:
        try:
            sent = await bills.send_due(transport, datetime.now(UTC))
            if sent:
                LOG.info("sent %d bill reminders", sent)
        except Exception as error:
            LOG.warning("reminders failed with %s", type(error).__name__)
        await asyncio.sleep(CHECK_SECONDS)
