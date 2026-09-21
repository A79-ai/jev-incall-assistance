"""The transcript and dashboard contracts; no generated free-form schema."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,80}$")]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    turn_id: Identifier
    speaker_role: Literal["buyer", "seller", "participant"] = "buyer"
    start_ms: int = Field(default=0, ge=0)
    text: str = Field(min_length=1, max_length=12000)
    revision: int = Field(default=1, ge=1)
    final: bool = True


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    meeting_id: Identifier
    transcript_version: int = Field(ge=0)
    turns: list[Turn] = Field(max_length=2000)


class CreateMeeting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    framework: Literal["meddpicc", "bant", "sentiment"] = "meddpicc"
