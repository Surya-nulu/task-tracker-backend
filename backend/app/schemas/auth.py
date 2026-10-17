import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# bcrypt only reads the first 72 bytes of a password; longer input would be
# silently truncated, so it is rejected instead.
BCRYPT_MAX_BYTES = 72


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(..., examples=["a@x.com"])
    password: str = Field(..., min_length=8, max_length=64, examples=["password123"])

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, value: str) -> str:
        if len(value.encode("utf-8")) > BCRYPT_MAX_BYTES:
            raise ValueError("password must be at most 72 bytes when UTF-8 encoded")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(..., examples=["a@x.com"])
    # No min_length here: login must not reveal password rules, only succeed or fail.
    password: str = Field(..., min_length=1, max_length=64, examples=["password123"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds", examples=[3600])


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime