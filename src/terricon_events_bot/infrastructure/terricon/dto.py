from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TerriconDTO(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class TerriconLocalizedTextDTO(TerriconDTO):
    ru: str
    kz: str


class TerriconDateDTO(TerriconDTO):
    unix: int
    iso: datetime
    text: str


class TerriconFormatDTO(TerriconDTO):
    id: str
    formated: str


class TerriconCardInfoDTO(TerriconDTO):
    name: str
    complete_url: str
    language: list[str]
    description: str
    url_live: str
    to_whom: str
    date_start: TerriconDateDTO


class TerriconLeadsInfoDTO(TerriconDTO):
    url_photo: str
    format: TerriconFormatDTO
    tags: list[str]
    status: str


class TerriconContactDTO(TerriconDTO):
    first_name: str
    last_name: str
    full_name: str
    role: str


class TerriconEventDTO(TerriconDTO):
    id: int = Field(gt=0, strict=True)
    uid: str
    type: str
    type_text: TerriconLocalizedTextDTO
    card_type: str
    url: str
    event_type: str = Field(alias="eventType")
    address: str
    short_description: str = Field(alias="shortDesc")
    card_info: TerriconCardInfoDTO
    leads_info: TerriconLeadsInfoDTO
    contact: TerriconContactDTO
