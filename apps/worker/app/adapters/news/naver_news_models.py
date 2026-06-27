from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NaverNewsSearchItemPlaceholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    originallink: str | None = None
    link: str | None = None
    description: str | None = None
    pubDate: str | None = None

