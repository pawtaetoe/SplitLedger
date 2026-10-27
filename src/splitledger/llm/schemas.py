from pydantic import BaseModel


# smoke test result for the fixture recorder
class SmokeResult(BaseModel):
    ok: bool
    text: str = ""
