# Phase 2 — FastAPI Foundation (implementation guide)

Everything needed to build Phase 2 yourself. Derived from `plan.md` lines 317–382 and 1160–1196.

**Goal:** a running FastAPI app with settings, an async DB session dependency, a consistent error
envelope, CORS, `GET /api/health`, and Swagger at `/docs`. No business logic.

**Prerequisites:** Phase 1 green (single Alembic head, `alembic check` clean), Postgres container up,
`backend/.venv` active. Every command below runs from `c:\task-tracker-backend\backend`.

---

## 0. Starting state

You already have these from Phase 1 / your own prep:

| Path | State | Action in Phase 2 |
|---|---|---|
| `app/core/config.py` | exists, `DATABASE_URL` only | **extend** with the Phase 2 fields |
| `app/core/database.py` | exists, complete | leave as-is |
| `app/core/exceptions.py` | empty file | fill in |
| `app/core/logging.py` | empty file | fill in |
| `app/schemas/common.py` | empty file | fill in |
| `app/routers/health.py` | empty file | fill in |
| `app/main.py` | empty file | fill in |
| `app/routers/__init__.py` | **missing** | create (empty) |
| `app/schemas/__init__.py` | **missing** | create (empty) |
| `.env` / `.env.example` | exist, `DATABASE_URL` only | add Phase 2 keys |
| `alembic/env.py` | already settings-driven | **no change** |

Also note: git currently sees `app/database.py` as deleted and `app/core/database.py` as untracked.
A `git add -A` resolves that into a single rename.

Build the files in dependency order: `config` → `logging` → `exceptions` → `schemas/common` →
`routers/health` → `main`.

---

## 1. Create the two missing package markers

```powershell
cd c:\task-tracker-backend\backend
New-Item -ItemType File -Force app\routers\__init__.py, app\schemas\__init__.py
```

Both stay empty. Without them the `app.routers` / `app.schemas` imports rely on namespace-package
behaviour, which trips up some linters and test collectors.

---

## 2. `app/core/config.py` — replace entirely

Every field a later phase needs lands here now, so `Settings` is written once and only ever gains
fields. `DATABASE_URL` and `JWT_SECRET` have no defaults, which is what makes the app fail fast.

```python
from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single source of truth for configuration. Later phases add fields, never replace this."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database --------------------------------------------------------
    DATABASE_URL: str

    # --- Auth (consumed from Phase 3) ------------------------------------
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- Redis / Celery (consumed from Phase 9) --------------------------
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: Optional[str] = None

    # --- Application -----------------------------------------------------
    API_PREFIX: str = "/api"
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: List[str] = ["http://localhost:5173"]

    # --- Alarm worker (consumed from Phase 10) ---------------------------
    ALARM_CHECK_INTERVAL_SECONDS: int = 60
    ALARM_BATCH_SIZE: int = 100

    @property
    def sync_database_url(self) -> str:
        """Driver swap for the Celery worker, which runs synchronously."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg")

    @property
    def celery_broker(self) -> str:
        """Falls back to REDIS_URL so dev only needs one Redis variable."""
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

> **`CORS_ORIGINS` gotcha.** pydantic-settings parses `list` fields as **JSON**, so if you set it in
> `.env` it must be `CORS_ORIGINS=["http://localhost:5173"]`. A bare
> `CORS_ORIGINS=http://localhost:5173` raises `SettingsError: error parsing value for field`.
> The in-code default already covers dev, so you can also just omit it from `.env`.

---

## 3. `app/core/logging.py`

```python
import logging
from logging.config import dictConfig


def configure_logging(level: str = "INFO") -> None:
    """Call once, before the app starts serving."""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                name: {"handlers": ["console"], "level": level, "propagate": False}
                for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```

---

## 4. `app/schemas/common.py`

```python
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Envelope used by every non-2xx response in this API."""

    detail: str = Field(..., examples=["Task not found"])
    code: str = Field(..., examples=["not_found"])


class ValidationErrorResponse(ErrorResponse):
    """422 adds FastAPI's structured error list."""

    errors: Optional[List[Any]] = None
```

---

## 5. `app/core/exceptions.py`

Services raise these; routers never build HTTP errors by hand. One set of handlers maps them to
responses, which keeps services testable without HTTP.

```python
import logging
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    """Base domain error. Subclasses fix status_code and code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        detail: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
    ) -> None:
        self.detail = detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail)


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}


def _envelope(status_code: int, detail: str, code: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail, "code": code, **extra},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _envelope(exc.status_code, exc.detail, exc.code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # jsonable_encoder: raw errors() can hold non-serialisable ctx objects.
        return _envelope(
            422,
            "Validation error",
            "validation_error",
            errors=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        log.warning("integrity error on %s %s", request.method, request.url.path)
        return _envelope(409, "Resource conflicts with existing data", "conflict")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers unknown routes (404) and wrong verbs (405).
        return _envelope(
            exc.status_code,
            str(exc.detail),
            _HTTP_CODES.get(exc.status_code, "http_error"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Traceback to the log only; the body never leaks internals.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _envelope(500, "Internal server error", "internal_error")
```

> The `StarletteHTTPException` handler is what makes `GET /api/nope` return the envelope instead of
> FastAPI's default `{"detail":"Not Found"}` — a Phase 2 checklist item.

---

## 6. `app/routers/health.py`

```python
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
```

Phase 9 extends this to ping Redis and return 503; Phase 19 splits it into `/health/live` and
`/health/ready`.

---

## 7. `app/main.py`

```python
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.routers import health

settings = get_settings()
log = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and dependency checks."},
    # auth / tasks / alarms tags arrive in Phases 3-5.
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup complete (environment=%s)", settings.ENVIRONMENT)
    yield  # Phase 12 starts the Redis subscriber task here.
    log.info("shutting down")
    await engine.dispose()


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="Task & Alarm API",
        version="0.2.0",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        # Swagger is dev-only; Phase 18 hardens this further.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(health.router, prefix=settings.API_PREFIX)

    return app


app = create_app()
```

---

## 8. `.env` and `.env.example`

Generate a secret first:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**`backend/.env`** (git-ignored — real values):

```dotenv
DATABASE_URL=postgresql+asyncpg://postgres:postgrespassword@localhost:5432/task_tracker
JWT_SECRET=paste-the-generated-secret-here
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
ENVIRONMENT=development
API_PREFIX=/api
CORS_ORIGINS=["http://localhost:5173"]
REDIS_URL=redis://localhost:6379/0
```

**`backend/.env.example`** (committed — placeholders only):

```dotenv
# Async driver URL, used by both the FastAPI app and Alembic.
# Inside Docker Compose the host becomes the service name (postgres), not localhost.
DATABASE_URL=postgresql+asyncpg://postgres:CHANGE_ME@localhost:5432/task_tracker

# Required, no default. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET=CHANGE_ME
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60

# development | production  (production disables /docs)
ENVIRONMENT=development
API_PREFIX=/api

# Must be valid JSON — a bare URL will fail to parse.
CORS_ORIGINS=["http://localhost:5173"]

# Used from Phase 9 onward.
REDIS_URL=redis://localhost:6379/0
```

---

## 9. Packages and `requirements.txt`

Phase 2 needs no new top-level library — `fastapi`, `uvicorn`, `sqlalchemy`, `asyncpg`, `pydantic`
and `pydantic-settings` are already installed. Two worthwhile additions:

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1

# Better --reload (watchfiles) + the websockets dep Phase 13 needs anyway
pip install "uvicorn[standard]==0.28.0"

# Optional: pull Phase 3's deps forward, as plan.md's Phase 2 command does
pip install "email-validator==2.1.1" "pyjwt==2.8.0" "bcrypt==4.1.3"
```

Then edit `requirements.txt` by hand to keep it pinned and free of transitive noise:

```text
fastapi==0.110.0
uvicorn[standard]==0.28.0
sqlalchemy==2.0.28
asyncpg==0.29.0
alembic==1.13.1
pydantic==2.6.4
pydantic-settings==2.2.1
python-dotenv==1.0.1
```

> Prefer `pip freeze > requirements.txt` as `plan.md` suggests only if you're happy listing ~20
> transitive packages. Hand-pinned direct dependencies are easier to audit in Phase 18.

Verify the install:

```powershell
pip check
python -c "import uvicorn, fastapi, pydantic_settings; print('ok')"
```

---

## 10. Alembic changes

**None.** Phase 1 already made `alembic/env.py` settings-driven, and it imports
`app.core.config` + `app.models` — never `database.py` — so moving that file changed nothing.

**One consequence to be aware of:** `env.py` calls `get_settings()`, and `JWT_SECRET` is now a
required field. Every Alembic command therefore needs `JWT_SECRET` present in `.env`, or it dies
with `ValidationError: JWT_SECRET Field required` before it ever reaches the database.

Confirm Phase 1 still holds after editing `Settings`:

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1
python -m alembic current      # cf24707d9568 (head)
python -m alembic check        # No new upgrade operations detected.
```

---

## 11. Run the application

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Expected tail:

```text
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [...] using WatchFiles
INFO:     Started server process [...]
2026-10-06 17:30:00 INFO     app.main | startup complete (environment=development)
INFO:     Application startup complete.
```

You must run from `backend/` — `pydantic-settings` resolves `env_file=".env"` relative to the
current working directory, and `app` must be importable from it.

---

## 12. Verification

Leave the server running; use a second terminal.

### Health check

```powershell
curl.exe -s http://localhost:8000/api/health
```
```json
{"status":"ok","database":"ok"}
```

### Swagger renders with the `health` tag

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" http://localhost:8000/docs
curl.exe -s http://localhost:8000/openapi.json | python -m json.tool | Select-String -Pattern '"title"|"version"|"health"'
```
Expect `200`, and the title/version/tag present. Then open http://127.0.0.1:8000/docs in a browser.

### Unknown route returns the envelope

```powershell
curl.exe -s http://localhost:8000/api/nope
```
```json
{"detail":"Not Found","code":"not_found"}
```

### Wrong verb returns the envelope

```powershell
curl.exe -s -X POST http://localhost:8000/api/health
```
```json
{"detail":"Method Not Allowed","code":"method_not_allowed"}
```

### Database down returns a generic 500

```powershell
docker stop task_tracker_db
curl.exe -s http://localhost:8000/api/health
docker start task_tracker_db
```
```json
{"detail":"Internal server error","code":"internal_error"}
```
The traceback must appear in the **uvicorn log only** — if any of it reaches the response body, the
`Exception` handler is wrong.

### CORS allows the Vite origin

```powershell
curl.exe -s -i -X OPTIONS http://localhost:8000/api/health `
  -H "Origin: http://localhost:5173" `
  -H "Access-Control-Request-Method: GET" | Select-String -Pattern "access-control-allow"
```
Expect `access-control-allow-origin: http://localhost:5173`.

### Fail-fast on missing config

```powershell
Rename-Item .env .env.bak
uvicorn app.main:app --port 8000     # ValidationError: JWT_SECRET / DATABASE_URL Field required
Rename-Item .env.bak .env
```

### 422 and AppError envelopes (optional, temporary)

Phase 2 has no endpoint that accepts input, so these two paths can't be exercised yet. To check them
now, paste this into `app/routers/health.py`, verify, then **delete it**:

```python
# TEMPORARY — delete before committing Phase 2.
from app.core.exceptions import NotFoundError


@router.get("/_debug/{number}")
async def _debug(number: int, fail: bool = False) -> dict:
    if fail:
        raise NotFoundError("Nothing here")
    return {"number": number}
```

```powershell
curl.exe -s "http://localhost:8000/api/health/_debug/abc"        # 422 validation_error + errors[]
curl.exe -s "http://localhost:8000/api/health/_debug/1?fail=true" # 404 {"detail":"Nothing here","code":"not_found"}
```

---

## 13. Commit

```powershell
cd c:\task-tracker-backend
git add -A
git status --short      # app/database.py -> app/core/database.py should show as R
git commit -m "feat(phase2): FastAPI foundation - settings, session dependency, error envelope, health"
```

Confirm `.env` is absent from `git status` and `.env.example` is staged.

---

## 14. Common errors

| Symptom | Cause / fix |
|---|---|
| `ValidationError: JWT_SECRET Field required` | `.env` missing or lacks the key. Also breaks every `alembic` command. |
| `SettingsError: error parsing value for field "CORS_ORIGINS"` | Must be JSON in `.env`: `["http://localhost:5173"]`. |
| `pydantic_settings` ignores `.env` | You're not in `backend/`. `env_file` is relative to the cwd. |
| `ModuleNotFoundError: No module named 'app'` | Same cause — run uvicorn from `backend/`. |
| `asyncpg.exceptions.InvalidPasswordError` | Wrong credentials in `DATABASE_URL`. |
| `ConnectionRefusedError` on `/api/health` | `docker start task_tracker_db`. |
| `/api/nope` returns `{"detail":"Not Found"}` with no `code` | `StarletteHTTPException` handler not registered. |
| Traceback text in a 500 response body | You registered the handler on `HTTPException` instead of `Exception`, or re-raised. |
| 500s raise through instead of returning JSON in tests | `TestClient(app, raise_server_exceptions=False)` — Phase 17. |
| `AttributeError: 'Settings' has no attribute 'CORS_ORIGINS'` | Field typo; `extra="ignore"` silently drops unknown `.env` keys but not code typos. |

---

## 15. Phase 2 completion checklist

From `plan.md` lines 378–382 and 1186–1196.

- [ ] `uvicorn app.main:app --reload` starts cleanly from `backend/`, no warnings
- [ ] `Settings` loads from `.env`; missing `JWT_SECRET`/`DATABASE_URL` fails fast with a clear error
- [ ] `get_db` yields an `AsyncSession` with `expire_on_commit=False`
- [ ] `/api/health` executes `SELECT 1` and reports `{"status":"ok","database":"ok"}`
- [ ] `/docs` renders and shows the `health` tag; `openapi.json` has `title`, `version`, tags
- [ ] Unknown route returns the JSON envelope with a `code`
- [ ] Forced exception returns the generic 500 envelope; traceback in logs only
- [ ] 422 validation responses carry `code: "validation_error"` plus the error list
- [ ] CORS allows `http://localhost:5173`
- [ ] `alembic current` still equals head and `alembic check` is clean
- [ ] Temporary `_debug` route removed
- [ ] `requirements.txt` committed; `.env` ignored; `.env.example` complete; no secrets in code
- [ ] You can explain, in your own words: why `expire_on_commit=False`, why the session is a
      dependency, and why services (not routers) will own `commit()`

**Gate:** do not start Phase 3 until every box is ticked.

---

## 16. What Phase 3 adds on top

For context only — don't build these yet. `core/security.py` (bcrypt + JWT), `schemas/auth.py`,
`services/auth_service.py`, `dependencies/auth.py`, `routers/auth.py`, and an `auth` entry in
`OPENAPI_TAGS`. The `AppError` subclasses you just wrote are exactly what `auth_service` raises:
`ConflictError` for a duplicate email, `UnauthorizedError` for bad credentials.
