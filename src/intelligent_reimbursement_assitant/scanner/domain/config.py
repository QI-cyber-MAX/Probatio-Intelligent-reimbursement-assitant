from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator


class ScannerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_file: Path
    unix_socket: Path | None = None
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 3310

    @field_validator("config_file", "unix_socket")
    @classmethod
    def absolute_paths(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value
