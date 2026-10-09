from typing import Any, List, Optional

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Envelope used by every non-2xx response in this API."""

    detail: str = Field(..., examples=["Task not found"])
    code: str = Field(..., examples=["not_found"])


class ValidationErrorResponse(ErrorResponse):
    """422 adds FastAPI's structured error list."""

    errors: Optional[List[Any]] = None