from typing import Annotated, Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import UnauthorizedError
from app.core.security import decode_access_token
from app.models import User

# auto_error=False: a missing header goes through our 401 envelope instead of
# FastAPI's built-in 403 "Not authenticated".
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Paste the access_token returned by POST /api/auth/login",
)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise UnauthorizedError("Not authenticated")

    user_id = decode_access_token(creds.credentials)
    user = await db.get(User, user_id)
    if user is None:
        raise UnauthorizedError("User no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]