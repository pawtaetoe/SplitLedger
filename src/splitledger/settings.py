from typing import Literal

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
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""
    public_base_url: str = ""
    meta_api_key: str = ""
    llm_base_url: str = "https://api.meta.ai/v1"
    llm_model: str = ""
    app_secret: str = ""


settings = Settings()
