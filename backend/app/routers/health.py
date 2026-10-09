from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    database: str


@router.get(
    "",
    response_model=HealthResponse,
    summary="Liveness and database check",
    responses={500: {"model": ErrorResponse, "description": "Database unreachable"}},
)
async def health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    # Deliberately not wrapped in try/except: a DB failure must surface as the
    # generic 500 envelope, with the traceback staying in the server log.
    await db.execute(text("SELECT 1"))
    return HealthResponse(status="ok", database="ok")