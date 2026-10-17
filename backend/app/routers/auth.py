from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.dependencies.auth import CurrentUser
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserRead
from app.schemas.common import ErrorResponse, ValidationErrorResponse
from app.services import auth_service

settings = get_settings()

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={
        409: {"model": ErrorResponse, "description": "Email already registered"},
        422: {"model": ValidationErrorResponse},
    },
)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> UserRead:
    user = await auth_service.register(db, body.email, body.password)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for an access token",
    responses={
        401: {"model": ErrorResponse, "description": "Invalid email or password"},
        422: {"model": ValidationErrorResponse},
    },
)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = await auth_service.authenticate(db, body.email, body.password)
    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get(
    "/me",
    response_model=UserRead,
    summary="The currently authenticated user",
    responses={401: {"model": ErrorResponse, "description": "Missing, invalid or expired token"}},
)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)