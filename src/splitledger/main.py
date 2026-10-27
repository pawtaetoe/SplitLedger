from fastapi import FastAPI

from splitledger.channels.simulator import dev_router
from splitledger.channels.twilio import twilio_router
from splitledger.channels.whatsapp import whatsapp_router
from splitledger.settings import settings

app = FastAPI(title="SplitLedger")
app.include_router(twilio_router if settings.whatsapp_provider == "twilio" else whatsapp_router)
if settings.env == "dev":
    app.include_router(dev_router)


# liveness probe
@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
