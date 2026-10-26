from fastapi import FastAPI

from splitledger.channels.simulator import dev_router
from splitledger.settings import settings

app = FastAPI(title="SplitLedger")
if settings.env == "dev":
    app.include_router(dev_router)


# liveness probe
@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
