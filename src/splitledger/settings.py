import re
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# all config from environment only
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://splitledger:splitledger@localhost:5432/splitledger"
    whatsapp_access_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_bot_number: str = ""
    whatsapp_provider: Literal["meta", "twilio"] = "meta"
    locale: str = "global"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    public_base_url: str = ""
    meta_api_key: str = ""
    llm_base_url: str = "https://api.meta.ai/v1"
    llm_model: str = ""
    # model for voice notes
    voice_model: str = "muse-voice-transcribe-1.0"
    app_secret: str = ""
    # exchangerate-api.com key, optional, free sources are used without it
    exchange_rate_api_key: str = ""
    # local hour from which bill reminders go out
    remind_hour: int = 9

    # hosts hand out postgres urls, the app reaches postgres through asyncpg
    @field_validator("database_url")
    @classmethod
    def async_driver(cls, url: str) -> str:
        return re.sub(r"^postgres(?:ql)?://", "postgresql+asyncpg://", url)


settings = Settings()
