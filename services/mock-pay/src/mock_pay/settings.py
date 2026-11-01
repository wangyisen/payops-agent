from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_PAY_", env_file=".env", extra="ignore")

    # A local-only default so `make mock-pay` works out of the box. It guards nothing real.
    api_key: SecretStr = SecretStr("dev-mock-pay-key")
