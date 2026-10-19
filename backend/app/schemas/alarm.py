import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AlarmRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    trigger_at: datetime
    is_triggered: bool
    created_at: datetime