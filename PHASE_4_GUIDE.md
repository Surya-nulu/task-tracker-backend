# Phase 4 — Task CRUD (implementation guide)

Everything needed to build Phase 4 yourself. Derived from `plan.md` lines 446–498, adapted to this
repo (UUID primary keys, the Phase 3 `CurrentUser` dependency, Windows PowerShell).

**Goal:** owner-scoped create / list / get / update / delete on tasks, plus
`PATCH /api/tasks/{id}/complete`. Every route requires a Bearer token.

**Prerequisites:** Phase 3 committed and green, Postgres running (`docker start task_tracker_db`),
`backend/.venv` active. Every command runs from `c:\task-tracker-backend\backend` unless stated.

---

## 0. Starting state and what changes

| Path | Action |
|---|---|
| `app/schemas/common.py` | **modify** — add the reusable `UtcDatetime` type |
| `app/schemas/alarm.py` | **create** — read-only `AlarmRead` (Phase 5 extends it) |
| `app/schemas/task.py` | **create** — `TaskStatus`, `TaskCreate`, `TaskUpdate`, `TaskRead` |
| `app/models/task.py` | **modify** — replace the `user_id` index with a list-query index |
| `alembic/versions/xxxx_task_list_index.py` | **generate** — one index swap |
| `app/services/task_service.py` | **create** — all task logic, owner-scoped |
| `app/routers/tasks.py` | **create** — the six endpoints |
| `app/main.py` | **modify** — `tasks` tag, router, `X-Total-Count` exposed to CORS |
| `requirements.txt`, `.env`, `.env.example` | **no change** |

### Endpoints (all require Bearer)

| Method | Path | Body → Response | Codes |
|---|---|---|---|
| POST | `/api/tasks` | `TaskCreate` → `TaskRead` | 201, 401, 422 |
| GET | `/api/tasks?status=&limit=&offset=` | → `TaskRead[]` + header `X-Total-Count` | 200, 401, 422 |
| GET | `/api/tasks/{task_id}` | → `TaskRead` | 200, 401, 404, 422 |
| PUT | `/api/tasks/{task_id}` | `TaskUpdate` → `TaskRead` | 200, 401, 404, 422 |
| PATCH | `/api/tasks/{task_id}/complete` | → `TaskRead` | 200, 401, 404, 422 |
| DELETE | `/api/tasks/{task_id}` | → empty | 204, 401, 404, 422 |

`TaskRead = {id, user_id, title, description, status, due_at, created_at, updated_at, alarms: AlarmRead[]}`

### Deviations from `plan.md` (because of this repo)

- **The migration is much smaller than `plan.md` describes.** `created_at`, `updated_at` and the
  `status` CHECK constraint already exist from Phase 1. The only database change is the list-query
  index.
- **The index is `(user_id, created_at, id)` ascending, not `(user_id, created_at DESC)`.**
  PostgreSQL scans a B-tree index backwards just as fast, so this serves
  `ORDER BY created_at DESC, id DESC` completely — including the `id` tie-breaker the plan's
  version can't cover. It also makes the old single-column `ix_tasks_user_id` redundant, so the
  migration drops it.
- **IDs are UUIDs.** A non-UUID in the path (`/api/tasks/1`) is a **422**; a well-formed UUID that
  isn't yours is a **404**.
- **`description` is exposed** (your model has it). **`is_daily` is not** — recurring tasks are
  postponed, and exposing a flag that does nothing invites bugs.
- **`PUT` is a partial update**, as the plan specifies (all fields optional). Only fields present in
  the body change. Sending `null` for `due_at` or `description` clears it; `null` for `title` or
  `status` is a 422.
- **The ownership helper is public** (`get_owned_task` instead of `_get_owned`) because Phase 5's
  `alarm_service` imports it.

---

## 1. Packages, `requirements.txt`, `.env`

**Nothing to install and no new settings.** Phase 4 only uses FastAPI, SQLAlchemy and Pydantic,
which are already pinned. Confirm the environment is intact:

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1
pip check
docker start task_tracker_db
python -m alembic current      # cf24707d9568 (head)
```

---

## 2. `app/schemas/common.py` — add `UtcDatetime`

Append to the existing file (keep `ErrorResponse` and `ValidationErrorResponse`). Update the imports
at the top so the whole file reads:

```python
from datetime import timezone
from typing import Annotated, Any, List, Optional

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field


class ErrorResponse(BaseModel):
    """Envelope used by every non-2xx response in this API."""

    detail: str = Field(..., examples=["Task not found"])
    code: str = Field(..., examples=["not_found"])


class ValidationErrorResponse(ErrorResponse):
    """422 adds FastAPI's structured error list."""

    errors: Optional[List[Any]] = None


# Rejects naive datetimes with a 422 and converts every accepted value to UTC,
# so the API stores and echoes UTC regardless of the offset the client sent.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda v: v.astimezone(timezone.utc))]
```

Phase 5's `AlarmCreate.trigger_at` reuses this type.

---

## 3. `app/schemas/alarm.py` — create

`TaskRead` embeds alarms, so a read schema has to exist now. Phase 5 adds `AlarmCreate`,
`AlarmUpdate`, `triggered_at` and `acknowledged_at`.

```python
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
```

---

## 4. `app/schemas/task.py` — create

```python
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
```

Why these choices:
- `str_strip_whitespace=True` makes `"   "` an empty string, which `min_length=1` then rejects.
- `use_enum_values=True` on `TaskUpdate` stores `"completed"`, not a `TaskStatus` object, on the
  ORM model.
- `model_fields_set` is how you tell "field omitted" (leave it alone) from "field sent as null".

---

## 5. `app/models/task.py` — swap the index

Two edits.

**a.** Add `Index` to the SQLAlchemy import:

```python
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
```

**b.** Add the index to `__table_args__`, and **remove `index=True`** from `user_id`:

```python
class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed')", name="status_valid"),
        # Serves "my tasks, newest first" (read backwards). Its leading user_id
        # column also covers the FK, so no separate user_id index is needed.
        Index("ix_tasks_user_id_created_at", "user_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
```

Leave everything else (columns, `owner`, `alarms` with `lazy="selectin"`) unchanged.

> The index needs an explicit name. Left to the naming convention it would be called
> `ix_tasks_user_id` — the same name as the index it replaces.

---

## 6. Alembic migration

```powershell
cd c:\task-tracker-backend\backend
python -m alembic revision --autogenerate -m "task list index"
```

Expected log lines:

```text
INFO  [alembic.autogenerate.compare] Detected added index 'ix_tasks_user_id_created_at' on '('user_id', 'created_at', 'id')'
INFO  [alembic.autogenerate.compare] Detected removed index 'ix_tasks_user_id' on 'tasks'
Generating ...\alembic\versions\<rev>_task_list_index.py ...  done
```

**Open the generated file and check it.** The body should be equivalent to this (quote style and
`op.f()` wrappers may differ):

```python
def upgrade() -> None:
    op.create_index("ix_tasks_user_id_created_at", "tasks", ["user_id", "created_at", "id"], unique=False)
    op.drop_index(op.f("ix_tasks_user_id"), table_name="tasks")


def downgrade() -> None:
    op.create_index(op.f("ix_tasks_user_id"), "tasks", ["user_id"], unique=False)
    op.drop_index("ix_tasks_user_id_created_at", table_name="tasks")
```

Red flags: anything touching `ck_tasks_status_valid`, `alarms`, `users`, or column types. If you see
those, the model edit went wrong — delete the generated file, fix the model, regenerate.

Apply, round-trip, and confirm:

```powershell
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
python -m alembic current      # <new rev> (head)
python -m alembic check        # No new upgrade operations detected.
docker exec task_tracker_db psql -U postgres -d task_tracker -c "\d tasks"
```

`\d tasks` must list `ix_tasks_user_id_created_at btree (user_id, created_at, id)` and **no**
`ix_tasks_user_id`.

---

## 7. `app/services/task_service.py` — create

Every function takes `(db, user_id, ...)`. There is no code path that loads a task without the
owner filter.

```python
import uuid
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models import Task
from app.schemas.task import TaskCreate, TaskStatus, TaskUpdate


async def get_owned_task(db: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    task = await db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
    if task is None:
        # 404, not 403: never confirm that another user's task exists.
        raise NotFoundError("Task not found")
    return task


async def create_task(db: AsyncSession, user_id: uuid.UUID, data: TaskCreate) -> Task:
    # alarms=[] marks the collection as loaded. Without it, serialising the
    # freshly committed task would lazy-load alarms, which async cannot do.
    task = Task(user_id=user_id, alarms=[], **data.model_dump())
    db.add(task)
    await db.commit()
    return task


async def list_tasks(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: Optional[TaskStatus],
    limit: int,
    offset: int,
) -> Tuple[List[Task], int]:
    filters = [Task.user_id == user_id]
    if status is not None:
        filters.append(Task.status == status.value)

    total = await db.scalar(select(func.count()).select_from(Task).where(*filters))
    rows = await db.scalars(
        select(Task)
        .where(*filters)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total or 0


async def get_task(db: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    return await get_owned_task(db, user_id, task_id)


async def update_task(
    db: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID, data: TaskUpdate
) -> Task:
    task = await get_owned_task(db, user_id, task_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    await db.commit()
    return task


async def complete_task(db: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID) -> Task:
    task = await get_owned_task(db, user_id, task_id)
    task.status = TaskStatus.COMPLETED.value
    await db.commit()
    return task


async def delete_task(db: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID) -> None:
    task = await get_owned_task(db, user_id, task_id)
    await db.delete(task)
    await db.commit()
```

Notes:
- **Alarms load automatically** on every `select(Task)` because of `lazy="selectin"` (one extra
  query per batch of tasks, not per task).
- **`updated_at` advances on update/complete** and is returned without a refresh, thanks to
  Phase 1's `eager_defaults=True`. If nothing actually changed, no UPDATE is sent and `updated_at`
  stays put — that's correct.
- **`exclude_unset=True`** is what makes `PUT` partial: omitted fields aren't in the dict.
- **Deleting a task deletes its alarms** — via the ORM cascade and the database's `ON DELETE CASCADE`.

---

## 8. `app/routers/tasks.py` — create

```python
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.dependencies.auth import CurrentUser
from app.schemas.common import ErrorResponse, ValidationErrorResponse
from app.schemas.task import TaskCreate, TaskRead, TaskStatus, TaskUpdate
from app.services import task_service

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    responses={
        401: {"model": ErrorResponse, "description": "Missing, invalid or expired token"},
        422: {"model": ValidationErrorResponse},
    },
)

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Task not found (or not yours)"}}


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED, summary="Create a task")
async def create_task(
    body: TaskCreate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TaskRead:
    task = await task_service.create_task(db, user.id, body)
    return TaskRead.model_validate(task)


@router.get("", response_model=List[TaskRead], summary="List your tasks, newest first")
async def list_tasks(
    response: Response,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    # Named status_filter so it doesn't shadow fastapi.status; still ?status= on the wire.
    status_filter: Optional[TaskStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> List[TaskRead]:
    tasks, total = await task_service.list_tasks(
        db, user.id, status=status_filter, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return [TaskRead.model_validate(task) for task in tasks]


@router.get("/{task_id}", response_model=TaskRead, summary="Get one task", responses=NOT_FOUND)
async def get_task(
    task_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TaskRead:
    task = await task_service.get_task(db, user.id, task_id)
    return TaskRead.model_validate(task)


@router.put(
    "/{task_id}",
    response_model=TaskRead,
    summary="Update a task (only the fields you send change)",
    responses=NOT_FOUND,
)
async def update_task(
    task_id: uuid.UUID, body: TaskUpdate, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TaskRead:
    task = await task_service.update_task(db, user.id, task_id, body)
    return TaskRead.model_validate(task)


@router.patch(
    "/{task_id}/complete",
    response_model=TaskRead,
    summary="Mark a task completed",
    responses=NOT_FOUND,
)
async def complete_task(
    task_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> TaskRead:
    task = await task_service.complete_task(db, user.id, task_id)
    return TaskRead.model_validate(task)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a task and its alarms",
    responses=NOT_FOUND,
)
async def delete_task(
    task_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> Response:
    await task_service.delete_task(db, user.id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

---

## 9. `app/main.py` — three edits

**a.** Import the router:

```python
from app.routers import auth, health, tasks
```

**b.** Add the tag (and drop the stale comment):

```python
OPENAPI_TAGS = [
    {"name": "health", "description": "Liveness and dependency checks."},
    {"name": "auth", "description": "Registration, login and the current user."},
    {"name": "tasks", "description": "Your tasks. Other users' tasks are invisible (404)."},
    # alarms tag arrives in Phase 5.
]
```

**c.** In `create_app()`: bump the version, expose the pagination header, include the router:

```python
        version="0.4.0",
```

```python
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Browsers hide non-standard response headers from JS unless exposed.
        expose_headers=["X-Total-Count"],
    )

    register_exception_handlers(app)
    app.include_router(health.router, prefix=settings.API_PREFIX)
    app.include_router(auth.router, prefix=settings.API_PREFIX)
    app.include_router(tasks.router, prefix=settings.API_PREFIX)
```

---

## 10. Run the application

```powershell
cd c:\task-tracker-backend\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Expect `Application startup complete.`. http://127.0.0.1:8000/docs now has a **tasks** section with
six operations, each with a lock icon.

---

## 11. Verification

Use a **second** PowerShell window. Run everything from `backend\` with the venv active.

### 11.1 Helpers (paste once per window)

`Api` is the Phase 3 helper with one addition: `-Headers` prints response headers too.
`Get-Token` registers the user if needed (ignoring "already registered") and returns a token.

```powershell
function Api {
    param([string]$Method, [string]$Path, [hashtable]$Body, [string]$Token, [switch]$Headers)
    $a = @('-s', '-w', '\nHTTP %{http_code}\n', '-X', $Method, "http://localhost:8000/api$Path")
    if ($Headers) { $a += '-i' }
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

function Get-Token([string]$Email, [string]$Password = 'password123') {
    $body = @{ email = $Email; password = $Password } | ConvertTo-Json
    try {
        Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/auth/register `
            -ContentType 'application/json' -Body $body | Out-Null
    } catch {}
    (Invoke-RestMethod -Method Post -Uri http://localhost:8000/api/auth/login `
        -ContentType 'application/json' -Body $body).access_token
}

$A = Get-Token 'alice@x.com'
$B = Get-Token 'bob@x.com'
```

Pattern used below: `($out = Api ...)` both **prints** the response and **stores** it, and
`($out[0] | ConvertFrom-Json).id` pulls the id from the JSON line.

### 11.2 Create → 201, `due_at` converted to UTC

```powershell
($out = Api POST /tasks @{ title = 'Complete project report'; due_at = '2026-10-02T22:30:00+05:30' } -Token $A)
$ID = ($out[0] | ConvertFrom-Json).id
$ID
```
```text
{"id":"…","user_id":"…","title":"Complete project report","description":null,"status":"pending","due_at":"2026-10-02T17:00:00Z","created_at":"…Z","updated_at":"…Z","alarms":[]}
HTTP 201
```
`22:30 +05:30` came back as `17:00Z`, `status` defaulted to `pending`, and `alarms` is `[]`.

### 11.3 Create validation → 422

```powershell
Api POST /tasks @{ title = 'Naive date'; due_at = '2026-10-02T22:30:00' } -Token $A
Api POST /tasks @{ title = '   ' } -Token $A
Api POST /tasks @{ title = ('x' * 201) } -Token $A
Api POST /tasks @{ title = 'Sneaky'; user_id = '00000000-0000-0000-0000-000000000000' } -Token $A
Api POST /tasks @{ title = 'Flag'; is_daily = $true } -Token $A
```
Each returns `"code":"validation_error"` and `HTTP 422` — naive datetime (`timezone_aware`), blank
title, title too long, `user_id` not accepted (`extra_forbidden`), `is_daily` not accepted.

### 11.4 No token → 401

```powershell
Api GET /tasks
Api POST /tasks @{ title = 'x' }
```
Both: `{"detail":"Not authenticated","code":"unauthorized"}`, `HTTP 401`.

### 11.5 List + `X-Total-Count`

```powershell
Api GET /tasks -Token $A -Headers | Select-String -Pattern 'x-total-count|HTTP/|^HTTP '
Api GET "/tasks?status=pending" -Token $A
Api GET "/tasks?status=completed" -Token $A
```
Expect `x-total-count: 1`, the task in the `pending` list, and `[]` for `completed`.

Bounds:

```powershell
Api GET "/tasks?limit=0" -Token $A        # 422
Api GET "/tasks?limit=101" -Token $A      # 422
Api GET "/tasks?offset=-1" -Token $A      # 422
Api GET "/tasks?status=bogus" -Token $A   # 422
```

### 11.6 Ordering and pagination

```powershell
Api POST /tasks @{ title = 'Second' } -Token $A | Out-Null
Start-Sleep -Milliseconds 50
Api POST /tasks @{ title = 'Third' } -Token $A | Out-Null
(Api GET /tasks -Token $A)[0] | ConvertFrom-Json | Select-Object title, created_at
(Api GET "/tasks?limit=1&offset=1" -Token $A)[0] | ConvertFrom-Json | Select-Object title
Api GET "/tasks?limit=1" -Token $A -Headers | Select-String 'x-total-count'
```
Expect `Third, Second, Complete project report` (newest first), the paged call returns only
`Second`, and `x-total-count: 3` even though one row was returned.

### 11.7 Get one → 200 / 404 / 422

```powershell
Api GET "/tasks/$ID" -Token $A                                       # 200
Api GET /tasks/00000000-0000-0000-0000-000000000000 -Token $A        # 404 Task not found
Api GET /tasks/1 -Token $A                                           # 422 (not a UUID)
```

### 11.8 Update (partial)

```powershell
Api PUT "/tasks/$ID" @{ title = 'Final project report' } -Token $A
```
Only `title` changes; `due_at` is still `2026-10-02T17:00:00Z`; `updated_at` is now later than
`created_at`.

```powershell
Api PUT "/tasks/$ID" @{ description = 'Include Q3 numbers'; due_at = '2026-10-05T09:00:00Z' } -Token $A
Api PUT "/tasks/$ID" @{ due_at = $null } -Token $A
```
The second call clears `due_at` to `null`.

Rejected updates — each `HTTP 422`:

```powershell
Api PUT "/tasks/$ID" @{ user_id = '00000000-0000-0000-0000-000000000000' } -Token $A
Api PUT "/tasks/$ID" @{ title = $null } -Token $A
Api PUT "/tasks/$ID" @{ status = 'bogus' } -Token $A
Api PUT "/tasks/$ID" @{ due_at = '2026-10-05T09:00:00' } -Token $A
```

### 11.9 Complete → 200

```powershell
Api PATCH "/tasks/$ID/complete" -Token $A
Api GET "/tasks?status=completed" -Token $A
```
`status` is `completed`, and the task now appears in the `completed` filter. Re-opening works too:

```powershell
Api PUT "/tasks/$ID" @{ status = 'pending' } -Token $A
```

### 11.10 Cross-user isolation → 404 everywhere

```powershell
Api GET "/tasks/$ID" -Token $B
Api PUT "/tasks/$ID" @{ title = 'hijacked' } -Token $B
Api PATCH "/tasks/$ID/complete" -Token $B
Api DELETE "/tasks/$ID" -Token $B
Api GET /tasks -Token $B -Headers | Select-String -Pattern 'x-total-count|^\['
```
The first four are all exactly `{"detail":"Task not found","code":"not_found"}` with `HTTP 404`.
Bob's list is `[]` with `x-total-count: 0`. Then confirm Alice's task is untouched:

```powershell
Api GET "/tasks/$ID" -Token $A      # title is NOT "hijacked"
```

### 11.11 Alarms are embedded

No alarm endpoints exist yet, so insert one directly:

```powershell
docker exec task_tracker_db psql -U postgres -d task_tracker -c "INSERT INTO alarms (id, task_id, trigger_at) VALUES (gen_random_uuid(), '$ID', now() + interval '1 hour');"
Api GET "/tasks/$ID" -Token $A
Api GET /tasks -Token $A
```
`alarms` now contains one object with `is_triggered: false`, in both the single and list responses —
and the server log shows no `MissingGreenlet`.

### 11.12 Delete → 204, cascades to alarms

```powershell
Api DELETE "/tasks/$ID" -Token $A          # empty body, HTTP 204
Api GET "/tasks/$ID" -Token $A             # 404
Api DELETE "/tasks/$ID" -Token $A          # 404 (already gone)
docker exec task_tracker_db psql -U postgres -d task_tracker -c "SELECT count(*) FROM alarms WHERE task_id = '$ID';"
```
The alarm count is `0`.

### 11.13 The list query uses the index (optional)

```powershell
docker exec task_tracker_db psql -U postgres -d task_tracker -c "SET enable_seqscan = off; EXPLAIN SELECT * FROM tasks WHERE user_id = gen_random_uuid() ORDER BY created_at DESC, id DESC LIMIT 50;"
```
Expect `Index Scan Backward using ix_tasks_user_id_created_at` and **no** `Sort` node.
(`enable_seqscan = off` is only there because a near-empty table would otherwise be scanned
sequentially, which is the right choice at that size.)

### 11.14 Swagger flow

1. http://127.0.0.1:8000/docs → **POST /api/auth/login** → copy `access_token`.
2. **Authorize** → paste → *Authorize* → *Close*.
3. **POST /api/tasks** → create → copy the `id`.
4. **GET /api/tasks** → the task is listed; the response headers show `x-total-count`.
5. **PUT /api/tasks/{task_id}** → change the title.
6. **PATCH /api/tasks/{task_id}/complete** → `status: completed`.
7. **DELETE /api/tasks/{task_id}** → 204.
8. **GET /api/tasks/{task_id}** → 404.

### 11.15 Earlier phases still hold

```powershell
curl.exe -s http://localhost:8000/api/health
Api GET /auth/me -Token $A
python -m alembic check
```

### 11.16 Clean up

```powershell
docker exec task_tracker_db psql -U postgres -d task_tracker -c "DELETE FROM users WHERE email LIKE '%@x.com';"
```
Users cascade to their tasks, which cascade to alarms.

---

## 12. Commit

```powershell
cd c:\task-tracker-backend
git add -A
git status --short     # new migration file included; .env NOT listed
git commit -m "feat(phase4): owner-scoped task CRUD with list index"
```

---

## 13. Common errors

| Symptom | Cause / fix |
|---|---|
| `MissingGreenlet` right after **creating** a task | `Task(...)` built without `alarms=[]`. |
| `MissingGreenlet` when listing/getting tasks | `lazy="selectin"` removed from `Task.alarms`. |
| Create echoes `"due_at":"…+05:30"` instead of `Z` | `due_at` typed as `AwareDatetime` instead of `UtcDatetime`. |
| Naive datetime accepted | Used `datetime` instead of `UtcDatetime` / `AwareDatetime`. |
| `PUT` with `{"title": null}` returns 409/500 | The `model_validator` is missing; the DB `NOT NULL` fired instead. |
| `PUT` wipes fields you didn't send | `model_dump()` without `exclude_unset=True`. |
| `TypeError: 'Query' object …` / `status` errors | Query parameter named `status` shadowing `fastapi.status`. Use `status_filter` + `alias="status"`. |
| `/api/tasks/1` returns 422, not 404 | Expected — IDs are UUIDs. |
| Autogenerate wants to drop/create `ck_tasks_status_valid` | Constraint `name=` changed. It must stay `"status_valid"`. |
| Autogenerate names the new index `ix_tasks_user_id` | The `Index(...)` has no explicit name. |
| `alembic check` reports the index after upgrade | Column list in the model and migration differ. Regenerate. |
| `X-Total-Count` missing in browser JS (Phase 8) | `expose_headers=["X-Total-Count"]` missing from CORS. |
| `updated_at` doesn't change | No field actually changed (no UPDATE sent) — correct. Raw `update()` statements must set it explicitly. |
| JSON body arrives mangled → 422 | PowerShell 5.1 + `curl.exe -d`. Use the `Api` helper. |

---

## 14. Security considerations

- **`user_id` comes only from the JWT.** It's absent from `TaskCreate` and `TaskUpdate`, and
  `extra="forbid"` turns an attempt to send it into a 422 (blocks mass assignment).
- **Cross-user access is a 404**, identical to a nonexistent task, so ids can't be probed.
- **Every query in `task_service` filters by owner.** `get_owned_task` is the only way to load a
  single task.
- **Bounded input:** `title` ≤ 200, `description` ≤ 2000, `limit` ≤ 100, `offset` ≥ 0.
- **UUID ids** aren't enumerable, so even a missed owner check wouldn't be trivially exploitable —
  but the owner check is still the real control.

---

## 15. Phase 4 completion checklist

From `plan.md` lines 493–498, plus the checks above.

- [ ] Migration swaps `ix_tasks_user_id` for `ix_tasks_user_id_created_at`; round-trips; `alembic check` clean
- [ ] `created_at` / `updated_at` present on tasks (from Phase 1) and returned in responses
- [ ] Create returns 201; `due_at` echoed in UTC (`Z`); `status` defaults to `pending`
- [ ] Naive `due_at`, blank/long title, `user_id`, `is_daily` in body → 422
- [ ] List is newest-first, filters by `status`, paginates, sends `X-Total-Count`; bad bounds → 422
- [ ] Get returns 200; unknown UUID → 404; non-UUID → 422
- [ ] Update changes only the fields sent; `null` clears `due_at`; `null` title / bad status → 422
- [ ] Complete sets `status: completed`; status can be set back to `pending`
- [ ] Delete returns 204, then 404; the task's alarms are gone
- [ ] User B gets 404 on A's task for GET / PUT / PATCH / DELETE; B's list is empty
- [ ] Tasks embed `alarms`; no `MissingGreenlet` in the server log
- [ ] Missing token → 401 on every route
- [ ] All six endpoints verified in Swagger
- [ ] Committed; `.env` not in git

**Gate:** do not start Phase 5 until every box is ticked.

---

## 16. What Phase 5 builds on

For context only. Phase 5 adds alarm endpoints nested under tasks. `alarm_service` calls
`task_service.get_owned_task` before creating an alarm, so ownership is enforced through the task.
It extends `AlarmRead` with `triggered_at` and `acknowledged_at`, adds the partial index
`ix_alarms_due`, and reuses `UtcDatetime` for `trigger_at`.
