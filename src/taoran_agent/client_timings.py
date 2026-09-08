"""Untrusted browser-relative durations, never used for score or task status."""
from pydantic import BaseModel, ConfigDict, Field


class ClientTimings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_received_ms: int | None = Field(default=None, ge=0, le=86400000)
    first_text_visible_ms: int | None = Field(default=None, ge=0, le=86400000)
    preview_complete_visible_ms: int | None = Field(default=None, ge=0, le=86400000)
    final_visible_ms: int | None = Field(default=None, ge=0, le=86400000)
    acknowledge_clicked_ms: int | None = Field(default=None, ge=0, le=86400000)
