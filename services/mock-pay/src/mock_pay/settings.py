from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: an empty MOCK_PAY_API_KEY= (as copied from .env.example) means "use the
    # default", not "the key is the empty string".
    model_config = SettingsConfigDict(
        env_prefix="MOCK_PAY_", env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    # A local-only default so `make mock-pay` works out of the box. It guards nothing real.
    api_key: SecretStr = SecretStr("dev-mock-pay-key")
