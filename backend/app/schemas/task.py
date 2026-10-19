import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.alarm import AlarmRead
from app.schemas.common import UtcDatetime


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"


class TaskCreate(BaseModel):
    # extra="forbid": a body containing user_id (or anything unknown) is a 422,
    # never silently ignored. Ownership comes only from the token.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=200, examples=["Complete project report"])
    description: Optional[str] = Field(None, max_length=2000)
    due_at: Optional[UtcDatetime] = Field(None, examples=["2026-10-02T22:30:00+05:30"])


class TaskUpdate(BaseModel):
    """Partial update: only the fields present in the request body change."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=True,
    )

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    due_at: Optional[UtcDatetime] = None
    status: Optional[TaskStatus] = None

    @model_validator(mode="after")
    def reject_null_for_required_columns(self) -> "TaskUpdate":
        for name in ("title", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    description: Optional[str]
    status: TaskStatus
    due_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    alarms: List[AlarmRead] = []