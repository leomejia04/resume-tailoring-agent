from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProfileOut(BaseModel):
    email: str | None
    display_name: str | None
    filename_prefix: str | None
    lock_version: int


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1, description="lock_version the client last saw")
    display_name: str | None = Field(default=None, max_length=100)
    filename_prefix: str | None = Field(
        default=None,
        max_length=40,
        description="Letters and digits used to name downloads, e.g. LeonardoSuzana",
    )

    @field_validator("display_name", "filename_prefix", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("filename_prefix")
    @classmethod
    def _plain_prefix(cls, value: str | None) -> str | None:
        if value is not None and not (
            value[0].isascii() and value[0].isalpha() and value.isascii() and value.isalnum()
        ):
            raise ValueError("use letters and digits only, starting with a letter")
        return value
