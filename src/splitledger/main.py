from fastapi import FastAPI

app = FastAPI(title="SplitLedger")


# liveness probe
@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
