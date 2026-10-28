import base64
import hashlib
import hmac
from datetime import datetime

# days a claim link keeps working
TOKEN_DAYS = 14


# signed claim links so a bill code alone can't be turned into a page
class ClaimTokens:
    # secret from settings, empty refuses to sign
    def __init__(self, secret: str, days: int = TOKEN_DAYS) -> None:
        self._secret = secret
        self._days = days

    # code, expiry and signature joined by dots
    def make(self, code: str, now: datetime) -> str:
        body = f"{code}.{int(now.timestamp()) + self._days * 86400}"
        return f"{body}.{self._sign(body)}"

    # the bill code when the token is genuine and not expired, else none
    def read(self, token: str, now: datetime) -> str | None:
        parts = token.split(".")
        if len(parts) != 3 or not parts[1].isdigit():
            return None
        code, expires, signature = parts
        if not hmac.compare_digest(signature, self._sign(f"{code}.{expires}")):
            return None
        return code if int(expires) >= now.timestamp() else None

    # a value with its signature, for cookies that must not be edited
    def seal(self, value: str) -> str:
        return f"{value}.{self._sign(value)}"

    # the value when its signature holds, else none
    def unseal(self, sealed: str) -> str | None:
        value, _, signature = sealed.rpartition(".")
        if not value or not hmac.compare_digest(signature, self._sign(value)):
            return None
        return value

    # short url safe hmac of the body
    def _sign(self, body: str) -> str:
        if not self._secret:
            raise RuntimeError("APP_SECRET must be set to make claim links")
        digest = hmac.new(self._secret.encode(), body.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest[:12]).decode()


# signed sign in values with an expiry, a short link from the chat then a longer cookie
class LoginLinks:
    # the signer and how long a value lasts
    def __init__(self, tokens: ClaimTokens, seconds: int) -> None:
        self._tokens = tokens
        self._seconds = seconds

    # the user id and expiry, signed
    def make(self, user_id: str, now: datetime) -> str:
        return self._tokens.seal(f"login:{user_id}.{int(now.timestamp()) + self._seconds}")

    # the user id when the value is genuine and not expired, else none
    def read(self, value: str, now: datetime) -> str | None:
        try:
            body = self._tokens.unseal(value)
        except RuntimeError:
            return None
        if body is None or not body.startswith("login:"):
            return None
        user_id, _, expires = body.removeprefix("login:").rpartition(".")
        if not expires.isdigit() or int(expires) < now.timestamp():
            return None
        return user_id or None
