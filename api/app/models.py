"""请求体的 Pydantic 模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RecordCreate(BaseModel):
    kind: Literal["raw", "derived"]
    detector: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    reading_mk: float | None = Field(default=None, ge=0)
    depends_on: list[str] = Field(default_factory=list, max_length=100)


class InvalidateRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=200)
