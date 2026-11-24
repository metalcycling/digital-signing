from pathlib import Path
import re
from pydantic import field_validator, model_validator, Field
from urllib.parse import urlsplit
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIGNING_", env_file=".env", extra="ignore")
    auth_mode: Literal['google','local'] = 'google'
    app_base_url: str = 'http://localhost:8000'
    auth_session_hours: int = Field(default=12, ge=1, le=168)

    @field_validator('app_base_url')
    @classmethod
    def valid_app_url(cls, value):
        value=value.rstrip('/')
        url=urlsplit(value)
        if url.scheme not in {'http','https'} or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError('Use an origin such as https://agreements.example.com with no path.')
        if url.scheme != 'https' and url.hostname not in {'localhost','127.0.0.1','::1','testserver'}:
            raise ValueError('Public hosting requires HTTPS.')
        return value

    @model_validator(mode='after')
    def local_auth_is_local(self):
        if self.auth_mode == 'local' and urlsplit(self.app_base_url).hostname not in {'localhost','127.0.0.1','::1','testserver'}:
            raise ValueError('Local authentication bypass cannot be used with a public hostname.')
        return self

    mode: Literal["demo", "google"] = "demo"
    email_mode: Literal["local", "gmail"] = "local"
    signing_timezone: str = "America/New_York"
    vendor_email: str = ""
    email_base_url: str = "http://localhost:8000"
    template_folder_id: str = "11K4hQcCiXVSq7GF2OEXQfYZ3ee9oXUFD"
    contracts_folder_id: str = ""
    data_dir: Path = Path.home() / ".local/share/digital-signing"

    @field_validator('signing_timezone')
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
        return value

    @field_validator('vendor_email')
    @classmethod
    def valid_vendor_email(cls, value):
        value = value.strip()
        if value and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value):
            raise ValueError('Enter a valid vendor notification email address.')
        return value

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
