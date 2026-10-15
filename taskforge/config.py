from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    debug: bool = True
    secret_key: str = "local-development-only-change-before-deployment"
    database_url: str = "sqlite:///db.sqlite3"
    redis_url: str = ""
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    jwt_ttl_minutes: int = Field(default=60, ge=1, le=1440)
    registration_open: bool = True
    http_allowed_hosts: str = ""
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    lease_grace_seconds: int = Field(default=15, ge=1, le=300)
    queue_republish_seconds: int = Field(default=60, ge=1, le=3600)

    @model_validator(mode="after")
    def production_secret(self):
        if not self.debug and (
            len(self.secret_key) < 32 or self.secret_key.startswith("local-development")
        ):
            raise ValueError("Set a unique SECRET_KEY of at least 32 characters with DEBUG=false")
        return self


env = Environment()
