# Phase 3 — Authentication (implementation guide)

Everything needed to build Phase 3 yourself. Derived from `plan.md` lines 386–444, adapted to this
repo (UUID primary keys, the Phase 2 `AppError` classes, Windows PowerShell).

**Goal:** register, login (JWT), `GET /api/auth/me`, and a reusable `get_current_user` dependency.

**Prerequisites:** Phase 2 committed and green, Postgres running (`docker start task_tracker_db`),
`backend/.venv` active. Every command runs from `c:\task-tracker-backend\backend` unless stated.

---

## 0. Starting state and what changes

| Path | Action |
|---|---|
| `app/core/security.py` | **create** — bcrypt hashing + JWT encode/decode |
| `app/schemas/auth.py` | **create** — request/response models |
| `app/services/__init__.py` | **create** (empty) |
| `app/services/auth_service.py` | **create** — register / authenticate |
| `app/dependencies/__init__.py` | **create** (empty) |
| `app/dependencies/auth.py` | **create** — `get_current_user`, `CurrentUser` |
| `app/routers/auth.py` | **create** — the three endpoints |
| `app/main.py` | **modify** — `auth` tag + include router |
| `requirements.txt` | **modify** — pin the three auth libraries |
| `.env` / `.env.example` | no new keys (Phase 2 already added them) |
| Alembic / database | **no change** — `users` table already has everything |

### Endpoints

| Method | Path | Body → Response | Codes |
|---|---|---|---|
| POST | `/api/auth/register` | `{email, password}` → `UserRead {id, email, created_at}` | 201, 409, 422 |
| POST | `/api/auth/login` | `{email, password}` → `{access_token, token_type, expires_in}` | 200, 401, 422 |
| GET | `/api/auth/me` | Bearer token → `UserRead` | 200, 401 |

### Deviations from `plan.md` (because of this repo)

- **`decode_access_token` returns `uuid.UUID`, not `int`** — your primary keys are UUIDs. `sub` is
  still `str(user_id)`.
- **bcrypt runs in a worker thread** (`asyncio.to_thread`). A bcrypt check takes ~250 ms of pure CPU;
  calling it directly inside an `async def` freezes every other request for that long.
- **Password limit is checked in bytes, not just characters.** bcrypt only uses the first 72 bytes;
  64 characters of non-ASCII text can exceed that. `bcrypt==4.1.3` truncates silently, so we reject.

---

## 1. Packages and `requirements.txt`

`bcrypt`, `PyJWT` and `email-validator` are **already installed** in your venv (you pulled them
forward in Phase 2) but are **not yet in `requirements.txt`**. Confirm, then pin:

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1
pip show bcrypt pyjwt email-validator | Select-String -Pattern "^Name|^Version"
```

If any is missing:

```powershell
pip install "bcrypt==4.1.3" "PyJWT==2.8.0" "email-validator==2.1.1"
```

Replace `requirements.txt` with:

```text
fastapi==0.110.0
uvicorn[standard]==0.28.0
sqlalchemy==2.0.28
asyncpg==0.29.0
alembic==1.13.1
pydantic==2.6.4
pydantic-settings==2.2.1
python-dotenv==1.0.1
email-validator==2.1.1
PyJWT==2.8.0
bcrypt==4.1.3
```

> Do **not** install `passlib` or `python-jose`. `passlib` is unmaintained and breaks on
> `bcrypt>=4.1` (`module 'bcrypt' has no attribute '__about__'`); `python-jose` is poorly maintained.

Verify:

```powershell
pip check
python -c "import bcrypt, jwt, email_validator; print('ok', jwt.__version__)"
```

---

## 2. Package markers

```powershell
New-Item -ItemType File -Force app\services\__init__.py, app\dependencies\__init__.py
```

---

## 3. `app/schemas/auth.py`

`UserRead` deliberately has no `hashed_password` field, so it can never be serialised.
`extra="forbid"` rejects unknown body keys with 422 instead of silently ignoring them.

```python
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
```

---

## 4. `app/core/security.py`

```python
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.exceptions import UnauthorizedError

settings = get_settings()

ACCESS_TOKEN_TYPE = "access"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # A malformed hash in the database is a failed login, never a 500.
        return False


def create_access_token(user_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            # An explicit allow-list: never let the token header choose the algorithm.
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat", "type"]},
        )
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Token has expired") from None
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token") from None

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise UnauthorizedError("Invalid token")

    try:
        return uuid.UUID(str(payload["sub"]))
    except ValueError:
        raise UnauthorizedError("Invalid token") from None
```

Notes:
- `ExpiredSignatureError` is a subclass of `InvalidTokenError`, so it must be caught **first**.
- `from None` hides the PyJWT traceback chain; the client only ever sees the envelope.

---

## 5. `app/services/auth_service.py`

```python
import asyncio

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import hash_password, verify_password
from app.models import User

# Checked against when the email is unknown, so "no such user" and "wrong
# password" both cost exactly one bcrypt verification.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def register(db: AsyncSession, email: str, password: str) -> User:
    email = normalise_email(email)

    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ConflictError("Email already registered")

    # bcrypt is CPU-bound (~250 ms); keep it off the event loop.
    hashed = await asyncio.to_thread(hash_password, password)
    user = User(email=email, hashed_password=hashed)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent registration of the same email.
        await db.rollback()
        raise ConflictError("Email already registered") from None
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = await db.scalar(select(User).where(User.email == normalise_email(email)))

    hashed = user.hashed_password if user is not None else _DUMMY_HASH
    password_ok = await asyncio.to_thread(verify_password, password, hashed)

    if user is None or not password_ok:
        raise UnauthorizedError("Invalid email or password")
    return user
```

Why it's shaped this way:
- **The service owns `commit()`**; the `get_db` dependency only owns the session's lifetime.
- **The pre-check gives a clean 409** in the normal case; the `IntegrityError` catch covers two
  simultaneous registrations, where only the database's unique index can decide.
- **`created_at` is available right after commit** without a refresh, because Phase 1's
  `TimestampMixin` sets `eager_defaults=True`, which fetches it via `RETURNING`.
- `_DUMMY_HASH` is computed once at import, which adds ~0.3 s to startup. That's expected.

---

## 6. `app/dependencies/auth.py`

```python
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
```

Every protected route from Phase 4 onward takes `user: CurrentUser` and uses `user.id`. That is the
**only** way a route learns who the caller is — never from the body, path, or query.

FastAPI caches dependencies per request, so a route that depends on both `CurrentUser` and `get_db`
receives the **same** session in both places.

---

## 7. `app/routers/auth.py`

Thin by design: parse the schema, call the service, return a schema.

```python
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
```

---

## 8. `app/main.py` — three edits

**a.** Import the router (next to `health`):

```python
from app.routers import auth, health
```

**b.** Add the tag:

```python
OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and dependency checks."},
    {"name": "auth", "description": "Registration, login and the current user."},
    # tasks / alarms tags arrive in Phases 4-5.
]
```

**c.** Bump the version and include the router in `create_app()`:

```python
        version="0.3.0",
```

```python
    register_exception_handlers(app)
    app.include_router(health.router, prefix=settings.API_PREFIX)
    app.include_router(auth.router, prefix=settings.API_PREFIX)
```

---

## 9. `.env` / `.env.example`

**No new keys.** Phase 3 consumes values Phase 2 already defined:

```dotenv
JWT_SECRET=<your generated secret>
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
```

Make sure `JWT_SECRET` is at least 32 characters (yours is 64). Short HS256 secrets are
brute-forceable offline from a single captured token. Never commit the real value; `.env.example`
keeps `JWT_SECRET=CHANGE_ME`.

---

## 10. Alembic

**No migration.** `users` already has `id` (UUID), `email` (unique index `ix_users_email`),
`hashed_password`, `created_at`, `updated_at`. Confirm nothing drifted:

```powershell
docker start task_tracker_db
python -m alembic current      # cf24707d9568 (head)
python -m alembic check        # No new upgrade operations detected.
```

If these fail with `ConnectionRefusedError: [WinError 1225]`, the container isn't running — that's
the `docker start` above, not an Alembic problem.

---

## 11. Run the application

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Expect `Application startup complete.` and no import errors. Open http://127.0.0.1:8000/docs —
there's now an **auth** section and an **Authorize** button at the top right.

---

## 12. Verification

Use a **second** PowerShell window (venv active, in `backend\`).

### 12.1 Helper (paste once per window)

Windows PowerShell 5.1 strips the double quotes from JSON passed to `curl.exe -d`, so this helper
writes the body to a temp file and prints the status code after the response.

```powershell
function Api {
    param([string]$Method, [string]$Path, [hashtable]$Body, [string]$Token)
    $a = @('-s', '-w', '\nHTTP %{http_code}\n', '-X', $Method, "http://localhost:8000/api$Path")
    if ($Token) { $a += @('-H', "Authorization: Bearer $Token") }
    $tmp = $null
    if ($Body) {
        $tmp = New-TemporaryFile
        $Body | ConvertTo-Json -Compress | Set-Content -Encoding ascii $tmp
        $a += @('-H', 'Content-Type: application/json', '--data-binary', "@$tmp")
    }
    curl.exe @a
    if ($tmp) { Remove-Item $tmp }
}
```

### 12.2 Register → 201

```powershell
Api POST /auth/register @{ email = 'A@X.com'; password = 'password123' }
```
```text
{"id":"3f0c...","email":"a@x.com","created_at":"2026-10-12T..."}
HTTP 201
```
Note the email came back **lowercased**, and there is no `hashed_password` field.

### 12.3 Duplicate register (different case) → 409

```powershell
Api POST /auth/register @{ email = 'a@X.COM'; password = 'password123' }
```
```text
{"detail":"Email already registered","code":"conflict"}
HTTP 409
```

### 12.4 Validation → 422

```powershell
Api POST /auth/register @{ email = 'not-an-email'; password = 'password123' }
Api POST /auth/register @{ email = 'b@x.com'; password = 'short' }
Api POST /auth/register @{ email = 'b@x.com'; password = 'password123'; is_admin = $true }
```
Each returns `"code":"validation_error"` with an `errors` list and `HTTP 422` (bad email, too short,
unknown field).

### 12.5 Login → 200, and capture the token

```powershell
Api POST /auth/login @{ email = 'ab@x.com'; password = 'password123' }

$TOKEN = (Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/auth/login `
    -ContentType 'application/json' `
    -Body (@{ email = 'ab@x.com'; password = 'password123' } | ConvertTo-Json)).access_token
$TOKEN
```
```text
{"access_token":"eyJhbGciOiJIUzI1NiIs...","token_type":"bearer","expires_in":3600}
HTTP 200
```

### 12.6 Wrong password and unknown email → identical 401

```powershell
Api POST /auth/login @{ email = 'a@x.com'; password = 'wrong-password' }
Api POST /auth/login @{ email = 'nobody@x.com'; password = 'password123' }
```
Both must be exactly:
```text
{"detail":"Invalid email or password","code":"unauthorized"}
HTTP 401
```

Timing should also be similar (both ~0.25–0.4 s), proving the dummy-hash path works:

```powershell
(Measure-Command { Api POST /auth/login @{ email = 'a@x.com'; password = 'wrong-password' } }).TotalMilliseconds
(Measure-Command { Api POST /auth/login @{ email = 'nobody@x.com'; password = 'password123' } }).TotalMilliseconds
```

### 12.7 `/me` with a valid token → 200

```powershell
Api GET /auth/me -Token $TOKEN
```
```text
{"id":"3f0c...","email":"a@x.com","created_at":"..."}
HTTP 200
```

### 12.8 `/me` without a token → 401

```powershell
Api GET /auth/me
```
```text
{"detail":"Not authenticated","code":"unauthorized"}
HTTP 401
```

### 12.9 Tampered token → 401

```powershell
$p = $TOKEN.Split('.')
$sig = $p[2].ToCharArray()
$sig[5] = if ($sig[5] -eq 'A') { 'B' } else { 'A' }
$BAD = "$($p[0]).$($p[1]).$(-join $sig)"
Api GET /auth/me -Token $BAD
```
```text
{"detail":"Invalid token","code":"unauthorized"}
HTTP 401
```

### 12.10 Expired token → 401

Rather than editing `.env`, mint an already-expired token with your real secret:

```powershell
$EXPIRED = python -c "import jwt, datetime as d; from app.core.config import get_settings; s = get_settings(); n = d.datetime.now(d.timezone.utc); print(jwt.encode({'sub': '00000000-0000-0000-0000-000000000000', 'type': 'access', 'iat': n - d.timedelta(hours=2), 'exp': n - d.timedelta(hours=1)}, s.JWT_SECRET, algorithm=s.JWT_ALGORITHM))"
Api GET /auth/me -Token $EXPIRED
```
```text
{"detail":"Token has expired","code":"unauthorized"}
HTTP 401
```

(`plan.md`'s alternative — set `ACCESS_TOKEN_EXPIRE_MINUTES=0`, restart, log in, wait 2 s, call
`/me` — also works. Remember to set it back to `60`.)

### 12.11 Wrong token type → 401

```powershell
$REFRESH = python -c "import jwt, datetime as d; from app.core.config import get_settings; s = get_settings(); n = d.datetime.now(d.timezone.utc); print(jwt.encode({'sub': '00000000-0000-0000-0000-000000000000', 'type': 'refresh', 'iat': n, 'exp': n + d.timedelta(hours=1)}, s.JWT_SECRET, algorithm=s.JWT_ALGORITHM))"
Api GET /auth/me -Token $REFRESH
```
Expect `"detail":"Invalid token"`, `HTTP 401`.

### 12.12 Deleted user → 401

```powershell
docker exec task_tracker_db psql -U postgres -d task_tracker -c "DELETE FROM users WHERE email = 'ab@x.com';"
Api GET /auth/me -Token $TOKEN
```
```text
{"detail":"User no longer exists","code":"unauthorized"}
HTTP 401
```
Re-run 12.2 and 12.5 afterwards if you want to keep testing.

### 12.13 Password stored as a bcrypt hash

```powershell
docker exec task_tracker_db psql -U postgres -d task_tracker -c "SELECT email, left(hashed_password, 7) AS prefix, length(hashed_password) AS len FROM users;"
```
Expect `prefix = $2b$12$` (bcrypt, cost 12) and `len = 60`. Never the plain password.

### 12.14 Swagger flow

1. http://127.0.0.1:8000/docs → **POST /api/auth/register** → *Try it out* → execute.
2. **POST /api/auth/login** → copy `access_token` (without quotes).
3. Click **Authorize** (top right) → paste the token → *Authorize* → *Close*.
4. **GET /api/auth/me** → *Try it out* → execute → 200 with your user.
5. *Authorize* → *Logout* → execute `/me` again → 401.

### 12.15 Phase 2 still holds

```powershell
curl.exe -s http://localhost:8000/api/health     # {"status":"ok","database":"ok"}
curl.exe -s http://localhost:8000/api/nope       # {"detail":"Not Found","code":"not_found"}
```

### 12.16 Clean up test users

```powershell
docker exec task_tracker_db psql -U postgres -d task_tracker -c "DELETE FROM users WHERE email LIKE '%@x.com';"
```

---

## 13. Commit

```powershell
cd c:\task-tracker-backend
git add -A
git status --short     # .env must NOT appear
git commit -m "feat(phase3): JWT authentication - register, login, me, get_current_user"
```

---

## 14. Common errors

| Symptom | Cause / fix |
|---|---|
| `ImportError: email-validator is not installed` | `pip install "email-validator==2.1.1"`. |
| `AttributeError: module 'bcrypt' has no attribute '__about__'` | `passlib` is installed or imported somewhere. Remove it; use `bcrypt` directly. |
| `/me` without token returns **403** instead of 401 | `HTTPBearer(auto_error=True)`. Set `auto_error=False`. |
| No **Authorize** button in Swagger | The route isn't depending on `HTTPBearer` (via `CurrentUser`). |
| Every login is 401 even with the right password | Email stored with different case before normalisation, or `hashed_password` set by hand. Re-register. |
| `ValueError: Invalid salt` in logs | A non-bcrypt value in `hashed_password` (manual SQL insert). `verify_password` turns it into a 401. |
| `InvalidSubjectError` | Only on PyJWT ≥ 2.10: `sub` must be a string — the code uses `str(user_id)`. |
| `MissingGreenlet` serialising the user | Something accessed `user.tasks`; `UserRead` must not include relationships. |
| Requests stall ~0.3 s each under load | bcrypt called without `asyncio.to_thread`. |
| JSON body arrives as `{email:a@x.com,...}` → 422 | PowerShell 5.1 stripped the quotes from `curl.exe -d`. Use the `Api` helper. |
| `ConnectionRefusedError: [WinError 1225]` | `docker start task_tracker_db`. |
| Startup takes ~0.3 s longer than Phase 2 | Expected — `_DUMMY_HASH` is computed at import. |

---

## 15. Security considerations (what this phase guarantees)

- bcrypt with the default cost (12); passwords bounded to 72 bytes.
- JWT expiry is mandatory (`require` includes `exp`); algorithm is an allow-list.
- Login failures are identical in message **and** timing for "no such user" vs "wrong password".
- Emails normalised to lowercase before both storage and lookup.
- `hashed_password` is never serialised — `UserRead` has no such field.
- Unknown users on login get **401**, never 404.
- Tokens only travel in the `Authorization` header — never in URLs.

Deferred: login rate limiting (Phase 18), refresh tokens and httpOnly cookies (postponed).

---

## 16. Phase 3 completion checklist

From `plan.md` lines 439–444, plus the checks above.

- [ ] `bcrypt`, `PyJWT`, `email-validator` pinned in `requirements.txt`; no `passlib` / `python-jose`
- [ ] Register returns 201 with `{id, email, created_at}`; email is lowercased
- [ ] Duplicate email (any case) returns 409
- [ ] Bad email / short password / unknown field return 422 with `validation_error`
- [ ] Login returns `{access_token, token_type:"bearer", expires_in:3600}`
- [ ] Wrong password and unknown email return the **same** 401 message with similar timing
- [ ] `/me` works with a valid token; 401 when missing, tampered, expired, wrong type, or user deleted
- [ ] `UserRead` never leaks `hashed_password`; DB stores `$2b$12$...` hashes
- [ ] Swagger: Authorize → paste token → `/me` works; Logout → 401
- [ ] `get_current_user` is the only way routes learn the user id
- [ ] `alembic check` still clean; `/api/health` still OK
- [ ] Committed; `.env` not in git

**Gate:** do not start Phase 4 until every box is ticked.

---

## 17. What Phase 4 builds on

For context only. Phase 4's task routes all take `user: CurrentUser` and pass `user_id=user.id`
into `task_service`, which filters every query by owner and returns **404** (not 403) for other
users' tasks. It also adds a migration for the `(user_id, created_at DESC)` index.
